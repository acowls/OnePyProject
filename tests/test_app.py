import os
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import subprocess
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QFont, QFontMetrics, QPalette
from PySide6.QtCore import QDate, QLocale, QPoint, QSettings, QSize, QTime, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QComboBox, QColorDialog, QDialog, QDoubleSpinBox, QFileDialog, QLineEdit, QMessageBox,
                               QPushButton, QStyle, QStyleOptionComboBox)

from onepyproject.app import (COLUMN_WIDTH_KEY, EMPTY_ROW_HINT, OPTIONAL_COLUMNS, SUMMARY_PALETTE, SUMMARY_PALETTE_NAMES,
                              TASK_COLUMN_KEYS, ExportDialog,
                              MainWindow, SettingsDialog, TaskDialog, collapsed_summary_color, display_time, summary_color)
from onepyproject.icons import ASSET_DIR, ICON_FILES, toolbar_icon
from onepyproject.model import Plan, Task, load_plan, save_plan, schedule
from onepyproject.mpp import import_mpp
from onepyproject.ui_preferences import DEFAULT_DENSITY, DENSITY_HEIGHTS, DENSITY_PREFERENCE_KEY


TEMPLATE = Path(__file__).resolve().parents[1] / "template.mpp"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def set_density_in_settings(window, density, monkeypatch):
    def choose(dialog):
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData(density))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", choose)
    window.settings()


def test_task_text_stays_dark_with_dark_system_palette(app):
    old_palette = app.palette()
    palette = QPalette(old_palette)
    palette.setColor(QPalette.ColorRole.Text, QColor("white"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("white"))
    app.setPalette(palette)
    try:
        window = MainWindow()
        window.plan.tasks = [Task(1, "Parent"), Task(2, "Child", parent=1)]
        window.show()
        window.refresh()
        app.processEvents()
        assert window.tree.palette().color(QPalette.ColorRole.Text) == QColor("#24354a")
        window.close()
    finally:
        app.setPalette(old_palette)


def test_save_as_onepyproject(app, tmp_path, monkeypatch):
    window = MainWindow()
    window.plan.tasks = [Task(1, "Planning")]
    window.dirty = True
    path = tmp_path / "schedule"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), "OnePyProject plan (*.pxplan)"))
    window.save()
    saved = tmp_path / "schedule.pxplan"
    assert load_plan(saved).tasks[0].name == "Planning"
    assert window.plan_path == saved
    assert not window.dirty


def test_inline_edit_undo_and_redo(app):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Planning")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    window.tree.editItem(window.items[1], 1)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    editor.selectAll()
    QTest.keyClicks(editor, "Renamed")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[0].name == "Renamed"
    assert window.dirty and window.undo_action.isEnabled()
    window.undo()
    assert window.plan.tasks[0].name == "Planning" and not window.dirty
    window.redo()
    assert window.plan.tasks[0].name == "Renamed" and window.dirty
    window.dirty = False
    window.close()


@pytest.mark.parametrize("modifier", [Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier])
def test_digits_begin_duration_entry_and_undo(app, tmp_path, modifier):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(tasks=[Task(1, "Work", duration=3, estimated=True)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    window.tree.setFocus()
    assert window.digit_edits_duration
    QTest.keyClick(window.tree, Qt.Key.Key_2, modifier)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None and editor.text() == "2"
    assert window.plan.tasks[0].duration == 3
    QTest.keyClicks(editor, "5")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[0].duration == 25
    assert not window.plan.tasks[0].estimated
    window.undo()
    assert window.plan.tasks[0].duration == 3 and window.plan.tasks[0].estimated
    window.dirty = False
    window.close()


def test_digit_duration_setting_persists_and_can_be_disabled(app, tmp_path, monkeypatch):
    path = str(tmp_path / "settings.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    assert window.digit_edits_duration
    def disable(dialog):
        assert dialog.digit_edits_duration.isChecked()
        dialog.digit_edits_duration.setChecked(False)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", disable)
    window.settings()
    assert not window.digit_edits_duration
    assert not window.dirty and not window._undo
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert not reopened.digit_edits_duration
    reopened.plan = Plan(tasks=[Task(1, "Work", duration=3)])
    reopened.reset_history(saved=True)
    reopened.show()
    reopened.refresh()
    reopened.tree.setCurrentItem(reopened.items[1], 1)
    reopened.tree.setFocus()
    QTest.keyClick(reopened.tree, Qt.Key.Key_2)
    app.processEvents()
    assert reopened.tree.findChild(QLineEdit) is None
    assert reopened.plan.tasks[0].duration == 3 and not reopened.dirty
    def enable(dialog):
        dialog.digit_edits_duration.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", enable)
    reopened.settings()
    assert MainWindow(QSettings(path, QSettings.Format.IniFormat)).digit_edits_duration
    reopened.close()


def test_task_deletion_confirmation_setting_persists_and_undo_restores_subtree(app, tmp_path, monkeypatch):
    path = str(tmp_path / "settings.ini")
    window = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Summary"), Task(2, "Child", parent=1),
                                                   Task(3, "Later", predecessors=[2])])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    questions = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: questions.append(args) or QMessageBox.StandardButton.No)
    window.delete_task()
    assert len(questions) == 1 and len(window.plan.tasks) == 3 and not window.dirty
    def disable(dialog):
        assert dialog.confirm_task_deletion.isChecked()
        dialog.confirm_task_deletion.setChecked(False)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", disable)
    window.settings()
    assert not window.confirm_task_deletion and not window.dirty and not window._undo
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert not reopened.confirm_task_deletion
    reopened.plan = deepcopy(window.plan)
    reopened.reset_history(saved=True)
    reopened.show()
    reopened.refresh()
    reopened.tree.setCurrentItem(reopened.items[1], 1)
    def unexpected(*args):
        raise AssertionError("Deletion confirmation is disabled")
    monkeypatch.setattr(QMessageBox, "question", unexpected)
    QTest.keyClick(reopened.tree, Qt.Key.Key_Delete)
    assert [(task.uid, task.predecessors) for task in reopened.plan.tasks] == [(3, [])]
    assert reopened.dirty and len(reopened._undo) == 1
    reopened.undo()
    assert [task.uid for task in reopened.plan.tasks] == [1, 2, 3]
    assert reopened.plan.tasks[-1].predecessors == [2] and not reopened.dirty
    reopened.redo()
    assert [task.uid for task in reopened.plan.tasks] == [3]
    reopened.undo()
    def enable(dialog):
        assert not dialog.confirm_task_deletion.isChecked()
        dialog.confirm_task_deletion.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", enable)
    reopened.settings()
    assert MainWindow(QSettings(path, QSettings.Format.IniFormat)).confirm_task_deletion
    reopened.close()
    window.close()


def test_cancelled_or_invalid_settings_do_not_change_preference(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    def cancel(dialog):
        dialog.digit_edits_duration.setChecked(False)
        dialog.confirm_task_deletion.setChecked(False)
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
        dialog.show_icon_labels.setChecked(True)
        dialog.date_format.setCurrentIndex(dialog.date_format.findData("short"))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(SettingsDialog, "exec", cancel)
    window.settings()
    assert window.digit_edits_duration
    assert window.confirm_task_deletion
    assert window.density == "comfortable" and not preferences.contains(DENSITY_PREFERENCE_KEY)
    assert not window.show_icon_labels
    assert window.date_format == "system"
    def invalid(dialog):
        dialog.digit_edits_duration.setChecked(False)
        dialog.confirm_task_deletion.setChecked(False)
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
        dialog.show_icon_labels.setChecked(True)
        dialog.date_format.setCurrentIndex(dialog.date_format.findData("short"))
        dialog.start.setText("invalid")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", invalid)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Ok)
    window.settings()
    assert window.digit_edits_duration
    assert window.confirm_task_deletion and not preferences.contains("editing/confirm_task_deletion")
    assert window.density == "comfortable" and not preferences.contains(DENSITY_PREFERENCE_KEY)
    assert not preferences.contains("keyboard/digit_edits_duration")
    assert not window.show_icon_labels and not preferences.contains("display/show_icon_labels")
    assert window.date_format == "system" and not preferences.contains("display/date_format")


@pytest.mark.parametrize("style", ["system", "short", "long"])
def test_date_formats_hide_time_for_long_tasks(app, tmp_path, style):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    preferences.setValue("display/date_format", style)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-11", tasks=[Task(1, "Full day"), Task(2, "Half day", duration=0.5),
                                                  Task(3, "Milestone", duration=0, predecessors=[1])])
    window.reset_history(saved=True)
    window.refresh()
    if style == "system":
        day = QLocale.system().toString(QDate(2026, 11, 11), QLocale.FormatType.ShortFormat)
        start_time = QLocale.system().toString(QTime(8, 0), QLocale.FormatType.ShortFormat)
        finish_time = QLocale.system().toString(QTime(12, 0), QLocale.FormatType.ShortFormat)
    elif style == "short":
        day, start_time, finish_time = "11 Nov", "08:00", "12:00"
    else:
        day, start_time, finish_time = "2026-11-11", "08:00", "12:00"
    assert window.items[1].text(3) == day
    assert window.items[1].text(4) == day
    assert window.items[2].text(3) == f"{day} {start_time}"
    assert window.items[2].text(4) == f"{day} {finish_time}"
    assert window.items[3].text(3) == day
    assert window.items[3].text(4) == day
    assert start_time in window.items[1].toolTip(3)
    assert window.tree.columnWidth(3) == window.tree.columnWidth(4)


@pytest.mark.parametrize("month,expected", [(1, "01 Jan"), (2, "01 Feb"), (11, "01 Nov"), (12, "01 Dec")])
def test_short_dates_include_space_and_keep_times(month, expected):
    value = datetime(2026, month, 1, 8, 30, 6)
    assert display_time(value, "short", include_time=False) == expected
    assert display_time(value, "short") == f"{expected} 08:30:06"


def test_settings_project_name_field_is_wide_and_resizable(app):
    title = "A longer project name for implementation and international rollout"
    dialog = SettingsDialog(Plan(title=title))
    dialog.show()
    app.processEvents()
    original_width = dialog.title.width()
    assert original_width >= 400 and dialog.title.text() == title
    dialog.resize(dialog.width() + 200, dialog.height())
    app.processEvents()
    assert dialog.title.width() >= original_width + 150
    dialog.close()


def test_settings_separates_project_and_user_preferences_and_holidays_are_multiline(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", holidays=["2026-12-25", "2027-01-01"], tasks=[Task(1, "Task")])
    window.reset_history(saved=True)
    window.refresh()
    def edit(dialog):
        assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ["Project-specific settings", "User preferences"]
        project, user = dialog.tabs.widget(0), dialog.tabs.widget(1)
        assert all(project.isAncestorOf(widget) for widget in (dialog.title, dialog.start, dialog.holidays))
        assert all(user.isAncestorOf(widget) for widget in (dialog.density_selector, dialog.date_format,
                                                              dialog.confirm_task_deletion, dialog.settings_location))
        assert user.isAncestorOf(dialog.color_collapsed_summaries)
        assert all(user.isAncestorOf(checkbox) for checkbox in dialog.column_visibility.values())
        assert dialog.holidays.toPlainText() == "2026-12-25\n2027-01-01"
        dialog.holidays.setPlainText("2026-12-24, 2026-12-25\n\n2027-01-01\n")
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", edit)
    window.settings()
    assert window.plan.holidays == ["2026-12-24", "2026-12-25", "2027-01-01"]
    assert window.density == "dense" and window.dirty and len(window._undo) == 1
    window.undo()
    assert window.plan.holidays == ["2026-12-25", "2027-01-01"] and not window.dirty
    assert window.density == "dense"
    window.close()


def test_invalid_multiline_holiday_does_not_save_preferences(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    def invalid(dialog):
        dialog.holidays.setPlainText("2026-12-25\nnot-a-date")
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", invalid)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.settings()
    assert errors and window.plan.holidays == [] and not window.dirty
    assert window.density == "comfortable" and not preferences.contains(DENSITY_PREFERENCE_KEY)
    window.close()


def test_optional_columns_and_summary_color_are_user_preferences(app, tmp_path, monkeypatch):
    path = str(tmp_path / "column-visibility.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    assert window.color_collapsed_summaries is True
    assert all(window.tree.isColumnHidden(TASK_COLUMN_KEYS.index(key)) is False
               for key, _label in OPTIONAL_COLUMNS)
    def choose(dialog):
        assert all(dialog.column_visibility[key].isChecked() for key, _label in OPTIONAL_COLUMNS)
        dialog.column_visibility["finish"].setChecked(False)
        dialog.column_visibility["work"].setChecked(False)
        dialog.color_collapsed_summaries.setChecked(False)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", choose)
    window.settings()
    assert window.tree.isColumnHidden(TASK_COLUMN_KEYS.index("finish"))
    assert window.tree.isColumnHidden(TASK_COLUMN_KEYS.index("work"))
    assert not window.tree.isColumnHidden(TASK_COLUMN_KEYS.index("resource"))
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert reopened.tree.isColumnHidden(TASK_COLUMN_KEYS.index("finish"))
    assert reopened.tree.isColumnHidden(TASK_COLUMN_KEYS.index("work"))
    assert not reopened.color_collapsed_summaries
    window.close()
    reopened.close()


def test_task_dialog_sets_and_clears_custom_summary_color(app, monkeypatch):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase"), Task(2, "Child", parent=1)])
    dialog = TaskDialog(plan.tasks[0], plan)
    assert not dialog.default_color_button.isHidden()
    assert dialog.default_color_button.toolTip() == "Default / Auto (blue bar)"
    assert len(dialog.summary_color_buttons) == 16 and dialog.custom_color_button.text() == "Custom"
    swatches = dialog.summary_color_row.findChildren(QPushButton)
    assert swatches[0] is dialog.default_color_button and swatches[-1] is dialog.custom_color_button
    assert swatches[1].toolTip() == SUMMARY_PALETTE_NAMES[0]
    monkeypatch.setattr(QColorDialog, "getColor", lambda *args: QColor("#f08040"))
    dialog.choose_summary_color()
    assert dialog.summary_color_value == "#f08040"
    assert "2px solid" in dialog.custom_color_button.styleSheet()
    dialog.apply(plan.tasks[0], plan)
    assert plan.tasks[0].summary_color == "#f08040"
    dialog.set_summary_color("")
    dialog.apply(plan.tasks[0], plan)
    assert plan.tasks[0].summary_color == ""


def test_task_dialog_widens_text_fields_and_places_estimate_next_to_duration(app):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase"), Task(2, "Child", parent=1)])
    dialog = TaskDialog(plan.tasks[0], plan)
    dialog.show()
    app.processEvents()
    assert dialog.name.width() >= 500
    assert dialog.resource.width() >= 500 and dialog.predecessors.width() >= 500
    assert dialog.duration.parentWidget() is dialog.estimated.parentWidget() is dialog.duration_row
    assert dialog.estimated.text() == "Estimated"
    dialog.close()


def test_completion_column_edits_tasks_and_rolls_up_summaries(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "progress.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase"),
        Task(2, "Short", parent=1, duration=2, percent_complete=75),
        Task(3, "Long", parent=1, duration=6, percent_complete=25)])
    window.reset_history(saved=True)
    window.refresh()
    assert window.items[1].text(9) == "37.5%"
    assert window.items[2].text(9) == "75.0%"
    assert not window.can_edit_cell(window.plan.tasks[0], 9)
    assert window.can_edit_cell(window.plan.tasks[1], 9)
    window.edit_cell(2, 9, "100%")
    assert window.plan.tasks[1].percent_complete == 100
    assert window.items[1].text(9) == "43.8%"
    window.undo()
    assert window.items[1].text(9) == "37.5%" and not window.dirty
    window.close()


def test_completion_editor_and_task_dialog_percent_field(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "completion-edit.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Summary"), Task(2, "Leaf", parent=1, percent_complete=35)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[2], 9)
    window.tree.editItem(window.items[2], 9)
    app.processEvents()
    editor = next(item for item in window.tree.findChildren(QDoubleSpinBox) if item.isVisible())
    assert editor.value() == 35 and editor.suffix() == " %"
    editor.setValue(80)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[1].percent_complete == 80 and window.items[1].text(9) == "80.0%"
    dialog = TaskDialog(window.plan.tasks[0], window.plan)
    assert dialog.percent_complete.value() == 80 and not dialog.percent_complete.isEnabled()
    dialog = TaskDialog(window.plan.tasks[1], window.plan)
    assert dialog.percent_complete.value() == 80 and dialog.percent_complete.isEnabled()
    window.dirty = False
    window.close()


def test_collapsed_summary_toggle_and_progress_gantt_overlays(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "summary-colors.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "SIT Phase"), Task(2, "Almost complete", parent=1, duration=4, percent_complete=50),
        Task(3, "Other", duration=2), Task(4, "SIT Phase 2"), Task(5, "Child", parent=4, duration=2)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    calls = []
    draw_summary = window.gantt.draw_summary_bar
    draw_progress = window.gantt.draw_completion
    def summary_spy(painter, item, y, name, expanded, progress, custom_color=""):
        calls.append((name, expanded, progress, custom_color))
        return draw_summary(painter, item, y, name, expanded, progress, custom_color)
    progress_calls = []
    def progress_spy(painter, item, y, percent, summary=False):
        progress_calls.append((percent, summary))
        return draw_progress(painter, item, y, percent, summary)
    monkeypatch.setattr(window.gantt, "draw_summary_bar", summary_spy)
    monkeypatch.setattr(window.gantt, "draw_completion", progress_spy)
    window.items[1].setExpanded(False)
    window.items[4].setExpanded(False)
    window.gantt.grab()
    assert calls == [("SIT Phase", False, 50, ""), ("SIT Phase 2", False, 0, "")]
    assert (50, True) in progress_calls
    assert window.color_collapsed_summaries
    assert collapsed_summary_color("SIT Phase", False) == "#72a9e0"
    assert collapsed_summary_color("SIT Phase", True) == collapsed_summary_color("SIT Phase 2", True)
    assert collapsed_summary_color("SIT Phase", False, "#ff0000") == "#ff0000"
    calls.clear()
    window.items[1].setExpanded(True)
    window.gantt.grab()
    assert calls[0][1] is True
    def disable_colors(dialog):
        assert dialog.color_collapsed_summaries.isChecked()
        dialog.color_collapsed_summaries.setChecked(False)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", disable_colors)
    window.settings()
    assert not window.color_collapsed_summaries
    assert preferences.value("display/color_collapsed_summaries", True, type=bool) is False
    window.dirty = False
    window.close()


def test_summary_color_setting_is_enabled_by_default_and_persistent(app, tmp_path):
    assert len(SUMMARY_PALETTE) == 16
    assert summary_color("SIT Phase") == summary_color("sit phase 2")
    assert summary_color("Café phase") == summary_color("Cafe phase")
    assert summary_color("café phase") in SUMMARY_PALETTE
    path = str(tmp_path / "summary-color.ini")
    window = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert window.color_collapsed_summaries
    dialog = SettingsDialog(window.plan, window, color_collapsed_summaries=True)
    assert dialog.color_collapsed_summaries.isChecked()
    dialog.color_collapsed_summaries.setChecked(False)
    dialog.accept()
    window.color_collapsed_summaries = dialog.color_collapsed_summaries.isChecked()
    window.preferences.setValue("display/color_collapsed_summaries", window.color_collapsed_summaries)
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert not reopened.color_collapsed_summaries
    reopened.close()
    window.close()


@pytest.mark.parametrize("density", ["comfortable", "tight", "dense"])
@pytest.mark.parametrize("dark", [False, True])
def test_mode_editor_fits_dense_rows_and_narrow_columns(app, tmp_path, density, dark):
    old_palette = app.palette()
    palette = QPalette(old_palette)
    if dark:
        palette.setColor(QPalette.ColorRole.Text, QColor("white"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("white"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#222222"))
        app.setPalette(palette)
    try:
        preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        preferences.setValue(DENSITY_PREFERENCE_KEY, density)
        window = MainWindow(preferences)
        window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Task", duration=2, resource="Alex")])
        window.reset_history(saved=True)
        window.show()
        window.refresh()
        window.tree.setColumnWidth(8, 45)
        app.processEvents()
        window.tree.horizontalScrollBar().setValue(window.tree.horizontalScrollBar().maximum())
        window.tree.setCurrentItem(window.items[1], 8)
        window.tree.editItem(window.items[1], 8)
        app.processEvents()
        editor = next((item for item in window.tree.findChildren(QComboBox) if item.isVisible()), None)
        assert editor is not None and editor.currentText() == "Duration"
        assert editor.accessibleName() == "Task scheduling mode"
        assert editor.width() >= editor.sizeHint().width() and editor.height() >= editor.sizeHint().height()
        assert window.tree.columnWidth(8) == 45
        option = QStyleOptionComboBox()
        editor.initStyleOption(option)
        text_rect = editor.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                                  QStyle.SubControl.SC_ComboBoxEditField, editor)
        assert text_rect.width() >= editor.fontMetrics().horizontalAdvance("Duration")
        assert text_rect.height() >= editor.fontMetrics().height()
        image = editor.grab().toImage()
        pixel = image.pixelColor(round(3 * image.devicePixelRatio()), round(3 * image.devicePixelRatio()))
        assert pixel.red() >= 245 and pixel.green() >= 245 and pixel.blue() >= 245
        editor.showPopup()
        app.processEvents()
        view = editor.view()
        item = editor.model().index(editor.findData("work"), 0)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         view.visualRect(item).center())
        app.processEvents()
        app.processEvents()
        assert window.plan.tasks[0].mode == "work" and window.plan.tasks[0].work == 16
        assert window.tree.columnWidth(8) == 45 and window.dirty and len(window._undo) == 1
        window.undo()
        assert window.plan.tasks[0].mode == "duration" and not window.dirty
        window.close()
    finally:
        app.setPalette(old_palette)


def test_mode_editor_keyboard_selection_commits_once(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Task", duration=2, resource="Alex")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 8)
    window.tree.editItem(window.items[1], 8)
    app.processEvents()
    editor = next(item for item in window.tree.findChildren(QComboBox) if item.isVisible())
    QTest.keyClick(editor, Qt.Key.Key_Down)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[0].mode == "work" and window.plan.tasks[0].work == 16
    assert len(window._undo) == 1
    window.undo()
    assert window.plan.tasks[0].mode == "duration" and not window.dirty
    window.close()


def test_summary_mode_remains_read_only(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(tasks=[Task(1, "Summary"), Task(2, "Child", parent=1)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 8)
    window.tree.editItem(window.items[1], 8)
    app.processEvents()
    assert not any(item.isVisible() for item in window.tree.findChildren(QComboBox))
    assert not window.dirty and not window._undo
    window.close()


def test_mode_editor_escape_cancels_and_unassigned_work_is_rejected(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    preferences.setValue(DENSITY_PREFERENCE_KEY, "dense")
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Unassigned", duration=2)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 8)
    window.tree.editItem(window.items[1], 8)
    app.processEvents()
    editor = next(item for item in window.tree.findChildren(QComboBox) if item.isVisible())
    editor.setCurrentIndex(editor.findData("work"))
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    app.processEvents()
    assert window.plan.tasks[0].mode == "duration" and not window.dirty and not window._undo
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.tree.editItem(window.items[1], 8)
    app.processEvents()
    editor = next(item for item in window.tree.findChildren(QComboBox) if item.isVisible())
    editor.showPopup()
    app.processEvents()
    view = editor.view()
    item = editor.model().index(editor.findData("work"), 0)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     view.visualRect(item).center())
    app.processEvents()
    app.processEvents()
    assert errors and "Assign a resource" in errors[-1]
    assert window.plan.tasks[0].mode == "duration" and not window.dirty and not window._undo
    window.close()


def test_toolbar_actions_are_icon_only_with_accessible_names(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    file_actions = [action for action in window.file_toolbar.actions() if action.text()]
    task_actions = [action for action in window.task_toolbar.actions() if action.text()]
    assert [window.control_tabs.tabText(i) for i in range(window.control_tabs.count())] == ["File", "Project"]
    assert window.control_tabs.widget(0) is window.file_toolbar
    assert window.control_tabs.widget(1) is window.task_toolbar
    assert window.control_tabs.currentIndex() == 1
    assert [action.text() for action in file_actions] == ["New", "Open", "Save", "Save As", "Import", "Export"]
    assert [action.text() for action in task_actions] == [
        "Settings", "Resources", "Assign", "+ Task", "Edit", "Delete", "Undo", "Redo", "Link", "Unlink", "Indent", "Outdent"]
    assert all(not action.icon().isNull() for action in file_actions + task_actions)
    for toolbar, actions in ((window.file_toolbar, file_actions), (window.task_toolbar, task_actions)):
        assert toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        for action in actions:
            button = toolbar.widgetForAction(action)
            assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
            assert button.accessibleName() == action.text()
            assert button.toolTip()
    assert not {"Zoom Out", "Zoom In"} & {action.text() for action in file_actions + task_actions}
    assert window.zoom_out_button.text() == "−" and window.zoom_in_button.text() == "+"
    assert window.zoom_out_button.accessibleName() == "Zoom out"
    assert window.zoom_in_button.accessibleName() == "Zoom in"
    assert window.zoom_out_button.parent() is window.statusBar()
    assert window.zoom_in_button.parent() is window.statusBar()
    assert window.ai_button.parent() is window.statusBar()
    assert window.ai_button.accessibleName() == "AI assistant" and not window.ai_button.icon().isNull()
    window.resize(1000, 700)
    window.show()
    app.processEvents()
    assert window.task_toolbar.isVisible() and not window.file_toolbar.isVisible()
    assert all(window.task_toolbar.widgetForAction(action).isVisible() for action in task_actions)
    window.control_tabs.setCurrentIndex(0)
    app.processEvents()
    assert window.file_toolbar.isVisible() and not window.task_toolbar.isVisible()
    assert window.zoom_out_button.isVisible()
    assert window.zoom_in_button.isVisible()
    assert window.ai_button.isVisible()
    assert window.zoom_in_button.x() < window.ai_button.x()
    window.close()


def test_toolbar_labels_are_optional_and_persist(app, tmp_path, monkeypatch):
    path = str(tmp_path / "preferences.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.resize(1000, 700)
    window.show()
    app.processEvents()
    assert not window.show_icon_labels
    def show_names(dialog):
        assert not dialog.show_icon_labels.isChecked()
        assert dialog.settings_location.text() == preferences.fileName()
        assert dialog.settings_location.isReadOnly()
        dialog.show_icon_labels.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", show_names)
    window.settings()
    app.processEvents()
    assert window.show_icon_labels
    assert window.file_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    assert window.task_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    assert all(window.file_toolbar.widgetForAction(action).toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
               for action in window.file_toolbar.actions())
    assert all(window.task_toolbar.widgetForAction(action).isVisible() for action in window.task_toolbar.actions())
    window.control_tabs.setCurrentIndex(0)
    app.processEvents()
    assert all(window.file_toolbar.widgetForAction(action).isVisible() for action in window.file_toolbar.actions())
    assert not window.dirty and not window._undo
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert reopened.show_icon_labels
    assert reopened.file_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    assert reopened.task_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    def hide_names(dialog):
        dialog.show_icon_labels.setChecked(False)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", hide_names)
    reopened.settings()
    hidden = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert hidden.file_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert hidden.task_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    window.close()


def test_default_columns_fit_requested_content(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    header_metrics = window.tree.header().fontMetrics()
    bold = QFont(window.tree.font())
    bold.setBold(True)
    data_metrics = QFontMetrics(bold)
    assert window.tree.columnWidth(2) == header_metrics.horizontalAdvance("Duration") + 14
    expected_date = max(data_metrics.horizontalAdvance("2020-08-08"), header_metrics.horizontalAdvance("Finish")) + 12
    assert window.tree.columnWidth(3) == window.tree.columnWidth(4) == expected_date
    assert window.tree.columnWidth(7) == max(data_metrics.horizontalAdvance("888 h"), header_metrics.horizontalAdvance("Work")) + 12
    assert window.tree.columnWidth(9) >= header_metrics.horizontalAdvance("Complete")


def test_task_column_widths_persist_across_processes_and_projects(app, tmp_path):
    path = str(tmp_path / "preferences.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Task")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    assert not any(preferences.contains(f"{COLUMN_WIDTH_KEY}/{key}") for key in TASK_COLUMN_KEYS)
    widths = [60, 330, 110, 125, 140, 170, 160, 85, 115, 90]
    for column, width in enumerate(widths):
        window.tree.setColumnWidth(column, width)
    assert window._column_width_sync.isActive()
    assert not window.dirty and not window._undo
    window.close()  # Flush even when closed before the debounce timer fires.
    assert not window._column_width_sync.isActive()
    assert [preferences.value(f"{COLUMN_WIDTH_KEY}/{key}", type=int) for key in TASK_COLUMN_KEYS] == widths
    code = """
import json, sys
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from onepyproject.app import MainWindow
from onepyproject.model import Plan, Task
app = QApplication([])
window = MainWindow(QSettings(sys.argv[1], QSettings.Format.IniFormat))
window.plan = Plan(tasks=[Task(8, "A different project")])
window.refresh()
window.resize(1800, 800)
window.show()
app.processEvents()
print(json.dumps([window.tree.columnWidth(i) for i in range(window.tree.columnCount())]))
"""
    result = subprocess.run([sys.executable, "-c", code, path], capture_output=True, text=True,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, check=True)
    assert json.loads(result.stdout) == widths
    other_user = MainWindow(QSettings(str(tmp_path / "other.ini"), QSettings.Format.IniFormat))
    assert other_user.tree.columnWidth(1) != widths[1]
    other_user.close()


def test_dragging_task_header_saves_column_width(app, tmp_path):
    path = str(tmp_path / "preferences.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.show()
    app.processEvents()
    header = window.tree.header()
    before = header.sectionSize(1)
    position = QPoint(header.sectionViewportPosition(1) + before - 1, header.height() // 2)
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
    QTest.mouseMove(header.viewport(), position + QPoint(53, 0), 10)
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       position + QPoint(53, 0))
    app.processEvents()
    assert header.sectionSize(1) == before + 53
    window.close()
    assert QSettings(path, QSettings.Format.IniFormat).value(f"{COLUMN_WIDTH_KEY}/task", type=int) == before + 53


def test_invalid_saved_column_widths_fall_back_individually(app, tmp_path):
    default = MainWindow(QSettings(str(tmp_path / "default.ini"), QSettings.Format.IniFormat))
    original = [default.tree.columnWidth(i) for i in range(default.tree.columnCount())]
    preferences = QSettings(str(tmp_path / "bad.ini"), QSettings.Format.IniFormat)
    for key, value in (("id", -1), ("task", "not-a-number"), ("duration", "0"),
                       ("start", 10001), ("finish", True), ("resource", 180), ("work", "9" * 5000)):
        preferences.setValue(f"{COLUMN_WIDTH_KEY}/{key}", value)
    window = MainWindow(preferences)
    widths = [window.tree.columnWidth(i) for i in range(window.tree.columnCount())]
    assert widths[:5] == original[:5]
    assert widths[6] == 180
    assert widths[5] == original[5] and widths[7:] == original[7:]
    assert not window._column_width_sync.isActive()
    default.close()
    window.close()


def test_column_widths_sync_after_debounce_and_warn_on_failure(app, monkeypatch):
    class FailingPreferences:
        def value(self, _key, default, type=None):
            return default
        def setValue(self, _key, _value):
            pass
        def sync(self):
            pass
        def status(self):
            return QSettings.Status.AccessError
        def fileName(self):
            return "/unwritable/preferences"
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    window = MainWindow(FailingPreferences())
    window.tree.setColumnWidth(1, 345)
    assert window._column_width_sync.isActive()
    QTest.qWait(350)
    assert not window._column_width_sync.isActive()
    assert warnings and "/unwritable/preferences" in warnings[-1]
    assert not window.dirty and not window._undo
    window.close()


def test_tree_branch_arrows_are_black_and_clickable_in_dark_mode(app, tmp_path):
    original = app.palette()
    palette = QPalette(original)
    palette.setColor(QPalette.ColorRole.Window, QColor("#303030"))
    palette.setColor(QPalette.ColorRole.Text, QColor("white"))
    app.setPalette(palette)
    try:
        window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
        window.plan = Plan(tasks=[Task(1, "Parent"), Task(2, "Child", parent=1)])
        window.reset_history(saved=True)
        window.show()
        window.refresh()
        app.processEvents()
        start = window.tree.header().sectionViewportPosition(1)
        y = window.tree.visualItemRect(window.items[1]).center().y()
        def black_pixels():
            image = window.tree.viewport().grab().toImage()
            scale = image.devicePixelRatio()
            return {(x, row) for x in range(round(start * scale), round((start + window.tree.indentation()) * scale))
                    for row in range(round((y - 6) * scale), round((y + 7) * scale))
                    if image.pixelColor(x, row).red() < 30 and image.pixelColor(x, row).green() < 30
                    and image.pixelColor(x, row).blue() < 30}
        opened = black_pixels()
        assert len(opened) > 5 and window.items[1].isExpanded()
        QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(start + window.tree.indentation() // 2, y))
        app.processEvents()
        closed = black_pixels()
        assert len(closed) > 5 and not window.items[1].isExpanded()
        assert closed != opened
        window.close()
    finally:
        app.setPalette(original)


def _contrast_ratio(first, second):
    def luminance(color):
        values = [color.redF(), color.greenF(), color.blueF()]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    a, b = sorted((luminance(first), luminance(second)))
    return (b + 0.05) / (a + 0.05)


@pytest.mark.parametrize("background", ["#222222", "#ffffff"])
def test_zoom_controls_have_contrast_in_light_and_dark_themes(app, tmp_path, background):
    original = app.palette()
    palette = QPalette(original)
    palette.setColor(QPalette.ColorRole.Window, QColor(background))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("white" if background == "#222222" else "black"))
    palette.setColor(QPalette.ColorRole.Text, QColor("white" if background == "#222222" else "black"))
    app.setPalette(palette)
    try:
        window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
        window.show()
        app.processEvents()
        for button in (window.zoom_out_button, window.zoom_in_button):
            colors = button.palette()
            assert _contrast_ratio(colors.color(QPalette.ColorRole.ButtonText),
                                   colors.color(QPalette.ColorRole.Button)) >= 4.5
        colors = window.statusBar().palette()
        assert _contrast_ratio(colors.color(QPalette.ColorRole.WindowText),
                               colors.color(QPalette.ColorRole.Window)) >= 4.5
        window.close()
    finally:
        app.setPalette(original)


@pytest.mark.parametrize("tab_index", [0, 1])
def test_undo_redo_shortcuts_work_with_either_tab_active(app, tmp_path, tab_index):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(tasks=[Task(1, "Original")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    window.control_tabs.setCurrentIndex(tab_index)
    window.edit_cell(1, 1, "Edited")
    window.tree.setFocus()
    undo_key = window.undo_action.shortcuts()[0][0]
    QTest.keyClick(window.tree, undo_key.key(), undo_key.keyboardModifiers())
    app.processEvents()
    assert window.plan.tasks[0].name == "Original"
    redo_key = window.redo_action.shortcuts()[0][0]
    QTest.keyClick(window.tree, redo_key.key(), redo_key.keyboardModifiers())
    app.processEvents()
    assert window.plan.tasks[0].name == "Edited"
    window.dirty = False
    window.close()


def test_flat_color_icons_render_at_retina_resolution(app):
    assert "MIT License" in (ASSET_DIR / "LICENSE.txt").read_text(encoding="utf-8")
    for name, filename in ICON_FILES.items():
        assert (ASSET_DIR / filename).is_file(), name
        icon = toolbar_icon(name)
        assert not icon.isNull(), name
        # SVG icon engines report no fixed sizes; a raster-only icon would be
        # scaled from a smaller source on Retina displays.
        assert not icon.availableSizes(), name
        image = icon.pixmap(QSize(20, 20), 2.0)
        assert image.width() == 40 and image.height() == 40, name
        assert image.devicePixelRatio() == 2.0, name


def test_gantt_zoom_preserves_schedule_and_view_center(app, tmp_path):
    settings_path = str(tmp_path / "settings.ini")
    window = MainWindow(QSettings(settings_path, QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Long project", duration=250)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    assert window.gantt.DAY == 26 and window.zoom_indicator.text() == "100%"
    zoom_width = window.zoom_indicator.width()
    assert zoom_width >= window.zoom_indicator.fontMetrics().horizontalAdvance("100%") + 10
    assert not window.zoom_in_button.isEnabled()
    window.zoom_in()
    assert window.gantt.DAY == 26
    window.tree.setCurrentItem(window.items[1], 1)
    original = window.calculated[1]
    bar = window.chart_area.horizontalScrollBar()
    bar.setValue(2000)
    width = window.chart_area.viewport().width()
    old_center = (bar.value() + width / 2) / window.gantt.DAY
    window.zoom_out_button.click()
    new_center = (bar.value() + width / 2) / window.gantt.DAY
    assert window.gantt.DAY == 18
    assert new_center == pytest.approx(old_center, abs=0.1)
    assert window.calculated[1] == original
    assert window.selected_uid() == 1 and not window.dirty and not window._undo
    assert window.zoom_indicator.text() == "69%"
    assert window.zoom_indicator.width() == zoom_width
    reopened = MainWindow(QSettings(settings_path, QSettings.Format.IniFormat))
    assert reopened.gantt.DAY == 18 and reopened.zoom_indicator.text() == "69%"
    window.zoom_in_button.click()
    assert window.gantt.DAY == 26 and window.zoom_indicator.text() == "100%"
    for _ in range(20):
        window.zoom_out()
        assert window.zoom_indicator.width() == zoom_width
    assert window.gantt.DAY == min(window.ZOOM_STEPS)
    assert not window.zoom_out_button.isEnabled()
    assert window.gantt.width() >= window.chart_area.viewport().width()
    app.processEvents()
    assert not window.gantt.grab().isNull()
    for _ in range(20):
        window.zoom_in()
        assert window.zoom_indicator.width() == zoom_width
    assert window.gantt.DAY == max(window.ZOOM_STEPS) == 26
    assert not window.zoom_in_button.isEnabled()
    assert window.calculated[1] == original and not window.dirty
    window.close()


def test_application_preferences_persist_together(app, tmp_path, monkeypatch):
    path = str(tmp_path / "preferences.ini")
    window = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    window.zoom_out()
    def change_preferences(dialog):
        dialog.show_icon_labels.setChecked(True)
        dialog.digit_edits_duration.setChecked(False)
        dialog.date_format.setCurrentIndex(dialog.date_format.findData("long"))
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", change_preferences)
    window.settings()
    assert not window.dirty and not window._undo
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert reopened.show_icon_labels and not reopened.digit_edits_duration
    assert reopened.date_format == "long" and reopened.density == "dense"
    assert reopened.zoom_indicator.text() == "69%"
    code = """
import json, sys
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from onepyproject.app import MainWindow
app = QApplication([])
window = MainWindow(QSettings(sys.argv[1], QSettings.Format.IniFormat))
print(json.dumps([window.show_icon_labels, window.digit_edits_duration,
                  window.date_format, window.density, window.zoom_indicator.text()]))
"""
    result = subprocess.run([sys.executable, "-c", code, path], capture_output=True, text=True,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, check=True)
    assert json.loads(result.stdout) == [True, False, "long", "dense", "69%"]


def test_layout_density_changes_rows_without_changing_plan(app, tmp_path, monkeypatch):
    settings_path = str(tmp_path / "settings.ini")
    window = MainWindow(QSettings(settings_path, QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "First"), Task(2, "Second")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    window.tree.setCurrentItem(window.items[2], 1)
    before = schedule(window.plan)
    dialog = SettingsDialog(window.plan, window, density=window.density)
    assert [dialog.density_selector.itemText(i) for i in range(dialog.density_selector.count())] == [
        "Comfortable", "Tight", "Dense"]
    assert DENSITY_HEIGHTS == {"comfortable": 24, "tight": 20, "dense": 18}
    assert window.density == DEFAULT_DENSITY == "comfortable"
    assert dialog.density_selector.currentData() == "comfortable"
    for density in ("comfortable", "tight", "dense"):
        set_density_in_settings(window, density, monkeypatch)
        app.processEvents()
        assert window.density == density
        assert window.tree.visualItemRect(window.items[1]).height() == DENSITY_HEIGHTS[density]
        assert window.tree.visualItemRect(window.items[2]).height() == DENSITY_HEIGHTS[density]
        assert schedule(window.plan) == before
        assert window.selected_uid() == 2 and not window.dirty and not window._undo
        image = window.gantt.grab().toImage()
        rect = window.tree.visualItemRect(window.items[2])
        x = int(window.gantt.x(window.calculated[2].start)) + 3
        y = window.tree.header().height() + rect.center().y()
        assert image.pixelColor(x, y) == QColor("#72a9e0")
    reopened = MainWindow(QSettings(settings_path, QSettings.Format.IniFormat))
    assert reopened.density == "dense"
    assert SettingsDialog(reopened.plan, density=reopened.density).density_selector.currentData() == "dense"
    window.close()


def test_unknown_saved_density_falls_back_to_comfortable(app, tmp_path):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    preferences.setValue(DENSITY_PREFERENCE_KEY, "unknown")
    window = MainWindow(preferences)
    assert window.density == "comfortable"
    assert SettingsDialog(window.plan, density=window.density).density_selector.currentData() == "comfortable"


def test_existing_density_preference_is_preserved(app, tmp_path):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    preferences.setValue("display/density", "comfortable")
    window = MainWindow(preferences)
    assert window.density == "comfortable"
    assert SettingsDialog(window.plan, density=window.density).density_selector.currentData() == "comfortable"


@pytest.mark.parametrize("previous,expected", [("comfortable", "comfortable"), ("tight", "comfortable"), ("dense", "tight")])
def test_old_density_preferences_keep_spacing_where_possible(app, tmp_path, previous, expected, monkeypatch):
    path = str(tmp_path / "density.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    preferences.setValue("display/density", previous)
    window = MainWindow(preferences)
    assert window.density == expected
    assert not preferences.contains(DENSITY_PREFERENCE_KEY)
    set_density_in_settings(window, "dense", monkeypatch)
    assert preferences.value("display/density") == previous
    assert preferences.value(DENSITY_PREFERENCE_KEY) == "dense"
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert reopened.density == "dense"
    window.close()
    reopened.close()


def test_preference_write_failure_is_reported(app, monkeypatch):
    class FailingPreferences:
        def value(self, _key, default, type=None):
            return default
        def setValue(self, _key, _value):
            pass
        def sync(self):
            pass
        def status(self):
            return QSettings.Status.AccessError
        def fileName(self):
            return "/unwritable/preferences"
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    window = MainWindow(FailingPreferences())
    set_density_in_settings(window, "dense", monkeypatch)
    assert warnings and "/unwritable/preferences" in warnings[-1]


def test_settings_density_switch_commits_active_cell_edit(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(tasks=[Task(1, "Original")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    window.tree.setCurrentItem(window.items[1], 1)
    window.tree.editItem(window.items[1], 1)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    editor.selectAll()
    QTest.keyClicks(editor, "Changed")
    def choose_density():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, SettingsDialog)
        dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
        dialog.accept()
    QTimer.singleShot(0, choose_density)
    window.settings()
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[0].name == "Changed" and window.dirty
    assert window.tree.visualItemRect(window.items[1]).height() == DENSITY_HEIGHTS["dense"]
    window.undo()
    assert window.plan.tasks[0].name == "Original"
    window.close()


def test_date_format_setting_updates_and_persists(app, tmp_path, monkeypatch):
    path = str(tmp_path / "settings.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    assert window.date_format == "system"
    window.plan = Plan(start="2026-11-11", tasks=[Task(1, "Work")])
    window.reset_history(saved=True)
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    def choose_short(dialog):
        assert [dialog.date_format.itemText(i) for i in range(dialog.date_format.count())] == [
            "System (default)", "11 Nov HH:MM", "YYYY-MM-DD HH:MM"]
        dialog.date_format.setCurrentIndex(dialog.date_format.findData("short"))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", choose_short)
    window.settings()
    assert window.items[1].text(3) == "11 Nov"
    assert window.selected_uid() == 1
    assert not window.dirty and not window._undo
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert reopened.date_format == "short"
    def choose_long(dialog):
        dialog.date_format.setCurrentIndex(dialog.date_format.findData("long"))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", choose_long)
    window.settings()
    assert window.items[1].text(3) == "2026-11-11"
    assert MainWindow(QSettings(path, QSettings.Format.IniFormat)).date_format == "long"


@pytest.mark.parametrize("uid", [1, 3])
def test_digit_shortcut_does_not_edit_calculated_durations(app, tmp_path, uid):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(tasks=[Task(1, "Summary"), Task(2, "Child", parent=1),
                              Task(3, "Fixed work", mode="work", resource="Alex")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[uid], 1)
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key.Key_9)
    app.processEvents()
    assert window.tree.findChild(QLineEdit) is None
    assert not window.dirty and not window._undo
    assert "calculated" in window.statusBar().currentMessage()
    window.close()


def test_digits_in_name_editor_do_not_change_duration(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(tasks=[Task(1, "Work", duration=3)])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    window.tree.editItem(window.items[1], 1)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    editor.selectAll()
    QTest.keyClicks(editor, "123")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[0].name == "123"
    assert window.plan.tasks[0].duration == 3
    window.dirty = False
    window.close()


def test_id_first_dates_and_estimated_duration(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Summary"), Task(2, "One day", parent=1, duration=1),
        Task(3, "Short", duration=0.5),
    ])
    window.reset_history(saved=True)
    window.refresh()
    assert window.tree.headerItem().text(0) == "ID"
    assert window.tree.headerItem().text(1) == "Task"
    assert window.tree.treePosition() == 1
    assert window.items[2].text(0) == "2"
    assert window.items[2].text(1) == "One day"
    expected_date = QLocale.system().toString(QDate(2026, 11, 16), QLocale.FormatType.ShortFormat)
    expected_time = QLocale.system().toString(QTime(8, 0), QLocale.FormatType.ShortFormat)
    assert window.items[2].text(3) == expected_date
    assert expected_time in window.items[2].toolTip(3)
    assert window.items[3].text(3).endswith(expected_time)
    window.edit_cell(2, 2, "2 days?")
    assert window.plan.tasks[1].estimated
    assert window.items[2].text(2) == "2 d?"
    assert window.items[1].text(2).endswith("?")
    saved = tmp_path / "estimated.pxplan"
    window.save_onepyproject(str(saved))
    assert load_plan(saved).tasks[1].estimated
    window.edit_cell(2, 2, "2d")
    assert not window.plan.tasks[1].estimated
    window.undo()
    assert window.plan.tasks[1].estimated


@pytest.mark.parametrize("empty_space", [False, True])
def test_double_click_empty_row_adds_and_undoes_task(app, empty_space):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Existing")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    rect = window.tree.visualItemRect(window.tree.placeholder)
    y = rect.bottom() + 50 if empty_space else rect.center().y()
    QTest.mouseDClick(window.tree.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier, QPoint(120, y))
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClicks(editor, "New task")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert [(task.uid, task.name) for task in window.plan.tasks] == [(1, "Existing"), (2, "New task")]
    window.undo()
    assert [task.name for task in window.plan.tasks] == ["Existing"]
    window.dirty = False
    window.close()


def test_empty_row_hint_includes_insert_shortcut_and_spans_grid(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.resize(1000, 700)
    window.show()
    app.processEvents()
    placeholder = window.tree.placeholder
    assert placeholder.text(0).strip() == EMPTY_ROW_HINT
    assert "Shift+Enter" in EMPTY_ROW_HINT
    assert placeholder.isFirstColumnSpanned()
    assert window.tree.fontMetrics().horizontalAdvance(placeholder.text(0)) < window.tree.viewport().width()
    assert "above the selected task" in placeholder.toolTip(0)
    window.close()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_enter_on_empty_row_starts_inline_entry(app, key):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Existing")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.tree.placeholder, 1)
    window.tree.setFocus()
    QTest.keyClick(window.tree, key)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClicks(editor, "Added")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert [task.name for task in window.plan.tasks] == ["Existing", "Added"]
    window.undo()
    assert [task.name for task in window.plan.tasks] == ["Existing"]
    window.dirty = False
    window.close()


def test_enter_in_empty_project_starts_inline_entry(app):
    window = MainWindow()
    window.show()
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key.Key_Return)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClicks(editor, "First")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert [task.name for task in window.plan.tasks] == ["First"]
    window.dirty = False
    window.close()


def test_clicking_blank_space_then_enter_adds_task(app):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Existing")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    rect = window.tree.visualItemRect(window.tree.placeholder)
    QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(120, rect.bottom() + 50))
    assert window.tree.currentItem() is window.tree.placeholder
    QTest.keyClick(window.tree, Qt.Key.Key_Return)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClicks(editor, "Added")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[-1].name == "Added"
    window.dirty = False
    window.close()


@pytest.mark.parametrize("selected,expected", [
    (1, [(5, 0), (1, 0), (2, 1), (3, 1), (4, 0)]),
    (3, [(1, 0), (2, 1), (5, 1), (3, 1), (4, 0)]),
])
def test_insert_above_preserves_outline_and_undo(app, selected, expected):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Summary"), Task(2, "Child A", parent=1),
                              Task(3, "Child B", parent=1, predecessors=[2]), Task(4, "Next")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[selected], 1)
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key.Key_Insert)
    app.processEvents()
    assert window.tree.insertion_before == selected
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClicks(editor, "Inserted")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert [(task.uid, task.parent) for task in window.plan.tasks] == expected
    assert next(task for task in window.plan.tasks if task.uid == 5).name == "Inserted"
    assert next(task for task in window.plan.tasks if task.uid == 3).predecessors == [2]
    assert [window.items[task.uid].text(0) for task in window.plan.tasks] == ["1", "2", "3", "4", "5"]
    assert window.items[3].text(5) == ("3" if selected == 1 else "2")
    window.undo()
    assert [(task.uid, task.parent) for task in window.plan.tasks] == [(1, 0), (2, 1), (3, 1), (4, 0)]
    window.redo()
    assert [(task.uid, task.parent) for task in window.plan.tasks] == expected
    window.dirty = False
    window.close()


def test_display_ids_and_predecessors_follow_insert_edit_save_undo_redo(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(10, "A"), Task(70, "B", predecessors=["10SS+0.5d"]),
        Task(20, "C", predecessors=["70FF-0.5d"]),
    ])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.add_inline_task("Inserted", before_uid=70)
    assert [task.uid for task in window.plan.tasks] == [10, 71, 70, 20]
    assert [window.items[task.uid].text(0) for task in window.plan.tasks] == ["1", "2", "3", "4"]
    assert window.items[20].text(5) == "3FF-0.5d"
    assert window.plan.tasks[-1].predecessors == ["70FF-0.5d"]
    window.edit_cell(20, 5, "2SS+2h, 3FF-0.5d")
    assert window.plan.tasks[-1].predecessors == ["71SS+2h", "70FF-0.5d"]
    assert window.items[20].text(5) == "2SS+2h, 3FF-0.5d"
    window.tree.setCurrentItem(window.items[20], 5)
    window.tree.editItem(window.items[20], 5)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None and editor.text() == "2SS+2h, 3FF-0.5d"
    editor.setText("2FS+50%")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[-1].predecessors == ["71FS+50%"]
    dialog = TaskDialog(window.plan.tasks[-1], window.plan)
    assert dialog.windowTitle() == "Task 4" and dialog.predecessors.text() == "2FS+50%"
    dialog.predecessors.setText("1SS+1h")
    assert window.mutate(lambda: dialog.apply(window.plan.tasks[-1], window.plan), 20)
    assert window.plan.tasks[-1].predecessors == ["10SS+1h"]
    window.undo()
    assert window.items[20].text(5) == "2FS+50%"
    window.redo()
    assert window.items[20].text(5) == "1SS+1h"
    path = tmp_path / "ids.pxplan"
    window.save_onepyproject(str(path))
    saved = load_plan(path)
    assert [task.uid for task in saved.tasks] == [10, 71, 70, 20]
    assert saved.tasks[-1].predecessors == ["10SS+1h"]
    saved_bytes = path.read_bytes()
    reopened = MainWindow(QSettings(str(tmp_path / "other-settings.ini"), QSettings.Format.IniFormat))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), "OnePyProject plan (*.pxplan)"))
    reopened.open_plan()
    assert [reopened.items[task.uid].text(0) for task in reopened.plan.tasks] == ["1", "2", "3", "4"]
    assert reopened.items[20].text(5) == "1SS+1h"
    assert not reopened.dirty and path.read_bytes() == saved_bytes
    reopened.close()
    window.close()


def test_invalid_display_predecessor_cannot_reference_an_internal_id(app, tmp_path, monkeypatch):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(10, "A"), Task(20, "B")])
    window.reset_history(saved=True)
    window.refresh()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.edit_cell(20, 5, "10SS+1d")
    assert errors and "Task ID 10 does not exist" in errors[-1]
    assert window.plan.tasks[-1].predecessors == [] and not window.dirty
    window.close()


def test_unchanged_predecessor_text_retains_original_precision(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    original = "10SS+1.234567890123456789e-3d"
    window.plan = Plan(start="2026-11-16", tasks=[Task(10, "A"), Task(90, "B", predecessors=[original])])
    window.reset_history(saved=True)
    window.refresh()
    shown = window.items[90].text(5)
    assert shown == "1SS+1.234567890123456789e-3d"
    window.edit_cell(90, 5, shown)
    assert window.plan.tasks[1].predecessors == [original] and not window.dirty
    dialog = TaskDialog(window.plan.tasks[1], window.plan)
    dialog.name.setText("Renamed")
    assert window.mutate(lambda: dialog.apply(window.plan.tasks[1], window.plan), 90)
    assert window.plan.tasks[1].predecessors == [original]
    window.dirty = False
    window.close()


def test_delete_and_reorder_update_display_ids_but_keep_dependency_targets(app, tmp_path, monkeypatch):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(10, "Summary"), Task(20, "A", parent=10),
                    Task(30, "B", parent=10, predecessors=["20SS+1h"]), Task(40, "Later")])
    window.reset_history(saved=True)
    window.refresh()
    window.tree.setCurrentItem(window.items[20], 1)
    window.outdent_task()
    assert [task.uid for task in window.plan.tasks] == [10, 30, 20, 40]
    assert window.items[30].text(0) == "2" and window.items[30].text(5) == "3SS+1h"
    assert next(task for task in window.plan.tasks if task.uid == 30).predecessors == ["20SS+1h"]
    window.tree.setCurrentItem(window.items[40], 1)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.delete_task()
    assert [window.items[task.uid].text(0) for task in window.plan.tasks] == ["1", "2", "3"]
    assert window.items[30].text(5) == "3SS+1h"
    window.undo()
    window.undo()
    assert [task.uid for task in window.plan.tasks] == [10, 20, 30, 40]
    assert window.items[30].text(5) == "2SS+1h"
    window.dirty = False
    window.close()


def test_collapsed_children_keep_their_display_numbers(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(10, "Summary"), Task(20, "Hidden child", parent=10),
                                                   Task(30, "Next", predecessors=[20])])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.items[10].setExpanded(False)
    window.add_inline_task("Inserted", before_uid=30)
    assert not window.items[10].isExpanded()
    assert [window.items[task.uid].text(0) for task in window.plan.tasks] == ["1", "2", "3", "4"]
    assert window.items[30].text(5) == "2"
    window.edit_cell(30, 5, "3")
    assert window.plan.tasks[-1].predecessors == [31]
    assert window.items[30].text(5) == "3"
    window.dirty = False
    window.close()


@pytest.mark.parametrize("key,modifiers", [
    (Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier),
    (Qt.Key.Key_Enter, Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.KeypadModifier),
])
def test_shift_enter_inserts_above_selected_task(app, key, modifiers):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "First"), Task(2, "Second")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[2], 1)
    window.tree.setFocus()
    QTest.keyClick(window.tree, key, modifiers)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None and window.tree.insertion_before == 2
    QTest.keyClicks(editor, "Inserted")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert [task.name for task in window.plan.tasks] == ["First", "Inserted", "Second"]
    window.undo()
    assert [task.name for task in window.plan.tasks] == ["First", "Second"]
    window.dirty = False
    window.close()


@pytest.mark.parametrize("key", [Qt.Key.Key_Escape, Qt.Key.Key_Return])
def test_cancel_or_blank_insert_leaves_plan_unchanged(app, key):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Existing")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    QTest.keyClick(window.tree, Qt.Key.Key_Insert)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    if key == Qt.Key.Key_Escape:
        QTest.keyClicks(editor, "Discarded")
    QTest.keyClick(editor, key)
    app.processEvents()
    app.processEvents()
    assert [task.name for task in window.plan.tasks] == ["Existing"]
    assert window.tree.insertion is None
    assert window.tree.currentItem() is window.items[1]
    assert not window.dirty and not window._undo
    window.close()


def test_insert_during_cell_edit_does_not_insert_a_task(app):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Existing")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    window.tree.editItem(window.items[1], 1)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClick(editor, Qt.Key.Key_Insert)
    assert window.tree.insertion is None
    assert len(window.plan.tasks) == 1
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    window.close()


def test_tab_and_shift_tab_indent_and_outdent(app):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "First"), Task(2, "Second")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setFocus()
    window.tree.setCurrentItem(window.items[2], 1)
    QTest.keyClick(window.tree, Qt.Key.Key_Tab)
    assert window.plan.tasks[1].parent == 1
    QTest.keyClick(window.tree, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert window.plan.tasks[1].parent == 0
    window.undo()
    assert window.plan.tasks[1].parent == 1
    window.dirty = False
    window.close()


@pytest.mark.parametrize("key", [Qt.Key.Key_Delete, Qt.Key.Key_Backspace])
def test_delete_key_removes_selected_task_and_undo_restores_it(app, monkeypatch, key):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "First"), Task(2, "Second", predecessors=[1])])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setFocus()
    window.tree.setCurrentItem(window.items[1], 1)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    QTest.keyClick(window.tree, key)
    assert [task.uid for task in window.plan.tasks] == [2]
    assert window.plan.tasks[0].predecessors == []
    window.undo()
    assert window.plan.tasks[1].predecessors == [1]
    window.dirty = False
    window.close()


def test_backspace_during_text_edit_does_not_delete_task(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "First")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    window.tree.editItem(window.items[1], 1)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    dialogs = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: dialogs.append(args))
    editor.setText("Test")
    editor.setCursorPosition(4)
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    assert editor.text() == "Tes"
    assert len(window.plan.tasks) == 1 and not dialogs
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    window.dirty = False
    window.close()


def test_tab_during_text_edit_does_not_indent_task(app):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "First"), Task(2, "Second")])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    window.tree.setCurrentItem(window.items[2], 1)
    window.tree.editItem(window.items[2], 1)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    app.processEvents()
    app.processEvents()
    assert window.plan.tasks[1].parent == 0
    window.dirty = False
    window.close()


def test_inline_validation_and_calculated_fields(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Phase"), Task(2, "Design", parent=1, resource="Alex", duration=2),
    ])
    window.reset_history(saved=True)
    window.refresh()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    assert not window.can_edit_cell(window.plan.tasks[0], 2)  # summary duration
    assert not window.can_edit_cell(window.plan.tasks[1], 4)  # calculated finish
    assert not window.can_edit_cell(window.plan.tasks[1], 7)  # calculated work
    window.edit_cell(2, 2, "-1")
    assert errors and window.plan.tasks[1].duration == 2
    assert not window._undo and not window.dirty
    window.edit_cell(2, 2, "1000000000")
    assert window.plan.tasks[1].duration == 2
    assert "100,000" in errors[-1]
    window.edit_cell(2, 3, "2026-11-18")
    assert window.calculated[2].start.date().isoformat() == "2026-11-18"
    window.edit_cell(2, 8, "work")
    assert window.plan.tasks[1].mode == "work"
    assert window.plan.tasks[1].work == 16
    assert not window.can_edit_cell(window.plan.tasks[1], 2)
    assert window.can_edit_cell(window.plan.tasks[1], 7)
    window.edit_cell(2, 7, "24 h")
    assert window.calculated[2].duration == 3
    window.edit_cell(2, 8, "duration")
    assert window.plan.tasks[1].duration == 3


def test_link_unlink_selection_and_cycle_rejection(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "A"), Task(2, "B", predecessors=[4]), Task(3, "C"), Task(4, "External"),
    ])
    window.reset_history(saved=True)
    window.refresh()
    for uid in (1, 2, 3):
        window.items[uid].setSelected(True)
    window.link_tasks()
    assert [task.predecessors for task in window.plan.tasks] == [[], [4, 1], [2], []]
    assert window.selected_uids() == [1, 2, 3]
    window.unlink_tasks()
    assert [task.predecessors for task in window.plan.tasks] == [[], [4], [], []]
    window.undo()
    assert window.plan.tasks[2].predecessors == [2]
    window.undo()
    assert window.plan.tasks[1].predecessors == [4] and not window.dirty
    window.redo()
    assert window.plan.tasks[2].predecessors == [2]
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.edit_cell(1, 5, "3")
    assert errors and window.plan.tasks[0].predecessors == []
    assert len(window._undo) == 1


def test_dependency_edit_link_unlink_and_delete(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "A"), Task(2, "B"), Task(3, "C")])
    window.reset_history(saved=True)
    window.refresh()
    window.edit_cell(2, 5, "1SS+3d")
    assert window.plan.tasks[1].predecessors == ["1SS+3d"]
    assert window.items[2].text(5) == "1SS+3d"
    window.items[1].setSelected(True)
    window.items[2].setSelected(True)
    window.link_tasks()  # The existing SS relationship must not be replaced or duplicated.
    assert window.plan.tasks[1].predecessors == ["1SS+3d"]
    window.unlink_tasks()
    assert window.plan.tasks[1].predecessors == []
    window.undo()
    assert window.plan.tasks[1].predecessors == ["1SS+3d"]
    window.tree.clearSelection()
    window.tree.setCurrentItem(window.items[1])
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.delete_task()
    assert window.plan.tasks[0].predecessors == []
    window.undo()
    assert window.plan.tasks[1].predecessors == ["1SS+3d"]


def test_inline_dependency_shows_hours_and_rejects_invalid_entry(app, tmp_path, monkeypatch):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "A"), Task(2, "B", duration=0.5)])
    window.reset_history(saved=True)
    window.refresh()
    window.edit_cell(2, 5, "1SS+3h")
    assert window.plan.tasks[1].predecessors == ["1SS+3h"]
    expected_time = QLocale.system().toString(QTime(11, 0), QLocale.FormatType.ShortFormat)
    assert window.items[2].text(3).endswith(expected_time)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.edit_cell(2, 5, "1XX+5d")
    assert errors and window.plan.tasks[1].predecessors == ["1SS+3h"]
    window.undo()
    assert window.plan.tasks[1].predecessors == []


def test_predecessor_without_lag_unit_is_normalized_and_undoable(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "A"), Task(2, "B"), Task(3, "C")])
    window.reset_history(saved=True)
    window.refresh()
    window.edit_cell(3, 5, "2SF+5")
    assert window.plan.tasks[2].predecessors == ["2SF+5d"]
    assert window.items[3].text(5) == "2SF+5d"
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.edit_cell(3, 5, "2SF+")
    assert errors and window.plan.tasks[2].predecessors == ["2SF+5d"]
    window.undo()
    assert window.plan.tasks[2].predecessors == []


def test_undo_savepoint_redo_branch_and_open_reset(app, tmp_path, monkeypatch):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Original")])
    window.reset_history(saved=True)
    window.refresh()
    window.edit_cell(1, 1, "First")
    window.save_onepyproject(str(tmp_path / "saved.pxplan"))
    assert not window.dirty
    window.edit_cell(1, 1, "Second")
    assert window.dirty
    window.undo()
    assert window.plan.tasks[0].name == "First" and not window.dirty
    window.redo()
    assert window.plan.tasks[0].name == "Second" and window.dirty
    window.undo()
    window.edit_cell(1, 1, "Third")
    assert not window._redo
    source = tmp_path / "other.pxplan"
    save_plan(Plan(title="Other", tasks=[Task(5, "New")]), source)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), "OnePyProject plan (*.pxplan)"))
    window.open_plan()
    assert window.plan.title == "Other" and not window.dirty
    assert not window._undo and not window._redo


def test_undo_indent_and_delete_restores_hierarchy_and_links(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(tasks=[Task(1, "Phase"), Task(2, "Work"), Task(3, "Next", predecessors=[2])])
    window.reset_history(saved=True)
    window.refresh()
    window.tree.setCurrentItem(window.items[2])
    window.indent_task()
    assert window.plan.tasks[1].parent == 1
    window.undo()
    assert window.plan.tasks[1].parent == 0
    window.redo()
    assert window.plan.tasks[1].parent == 1
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.delete_task()
    assert [task.uid for task in window.plan.tasks] == [1, 3]
    assert window.plan.tasks[1].predecessors == []
    window.undo()
    assert [task.uid for task in window.plan.tasks] == [1, 2, 3]
    assert window.plan.tasks[1].parent == 1
    assert window.plan.tasks[2].predecessors == [2]


def test_open_onepyproject_from_open_dialog(app, tmp_path, monkeypatch):
    source = tmp_path / "schedule.pxplan"
    save_plan(Plan(title="Saved project", tasks=[Task(1, "Planning")]), source)
    window = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), "Project files (*.pxplan *.mpp)"))
    window.open_plan()
    assert window.plan.title == "Saved project"
    assert window.plan_path == source
    assert window.import_path is None and not window.dirty


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_open_mpp_from_open_dialog_preserves_original(app, tmp_path, monkeypatch):
    source = tmp_path / "source.MPP"
    original = TEMPLATE.read_bytes()
    source.write_bytes(original)
    window = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), "Project files (*.pxplan *.mpp)"))
    warnings = []
    def accept_warning(*args):
        warnings.append(args[1])
        return QMessageBox.StandardButton.Ok
    monkeypatch.setattr(QMessageBox, "warning", accept_warning)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    window.open_plan()
    assert not errors
    assert warnings and warnings[0] == "Lossy MPP import"
    assert window.plan.tasks
    assert window.import_path == source
    assert window.plan_path is None and window.dirty
    assert "imported copy" in window.statusBar().currentMessage()
    assert source.read_bytes() == original


def test_cancel_mpp_import_keeps_current_plan(app, tmp_path, monkeypatch):
    source = tmp_path / "schedule.mpp"
    window = MainWindow()
    window.plan.tasks = [Task(1, "Existing")]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), "Microsoft Project (*.mpp)"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Cancel)
    window.open_plan()
    assert window.plan.tasks[0].name == "Existing"
    assert window.import_path is None


def test_invalid_mpp_keeps_current_plan(app, tmp_path, monkeypatch):
    source = tmp_path / "invalid.mpp"
    source.write_text("not an MPP", encoding="utf-8")
    window = MainWindow()
    window.plan.tasks = [Task(1, "Existing")]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), "Microsoft Project (*.mpp)"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Ok)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    window.open_plan()
    assert errors
    assert window.plan.tasks[0].name == "Existing"
    assert window.import_path is None


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_save_as_mpp_creates_copy_without_changing_working_save(app, tmp_path, monkeypatch):
    window = MainWindow()
    window.plan.tasks = [Task(1, "Planning"), Task(2, "Gate", duration=0, predecessors=[1])]
    window.dirty = True
    output = tmp_path / "schedule"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(output), "Microsoft Project (*.mpp)"))
    def choose_template(dialog):
        dialog.template_edit.setText(str(TEMPLATE))
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(ExportDialog, "exec", choose_template)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: QMessageBox.StandardButton.Ok)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    window.save()
    written = tmp_path / "schedule.mpp"
    assert not errors
    assert written.exists()
    imported, _ = import_mpp(written)
    assert [task.name for task in imported.tasks] == ["Planning", "Gate"]
    assert window.dirty and window.plan_path is None
    # Saving a second time cannot overwrite the first MPP copy.
    original = written.read_bytes()
    window.save_as()
    assert warnings and "does not overwrite" in warnings[-1] and written.read_bytes() == original


def test_fixed_units_edits_recalculate_the_other_value(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Staffed", duration=2, resource="Alex"), Task(2, "Unstaffed", mode="units", duration=2)])
    window.reset_history(saved=True)
    window.refresh()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.edit_cell(1, 8, "units")
    task = window.plan.tasks[0]
    assert task.mode == "units" and window.items[1].text(8) == "Units"
    assert window.can_edit_cell(task, 2) and window.can_edit_cell(task, 7)
    window.edit_cell(1, 7, "24 h")                       # work drives duration
    assert window.plan.tasks[0].duration == 3 and window.calculated[1].work == 24
    window.edit_cell(1, 2, "4")                          # duration drives work
    assert window.calculated[1].work == 32 and window.plan.tasks[0].mode == "units"
    window.edit_cell(1, 8, "work")                       # keeps the calculated work
    assert window.plan.tasks[0].work == 32
    window.edit_cell(1, 8, "units")                      # and the calculated duration coming back
    assert window.plan.tasks[0].duration == 4
    # Nothing assigned, so work cannot determine a duration.
    assert window.can_edit_cell(window.plan.tasks[1], 2) and not window.can_edit_cell(window.plan.tasks[1], 7)
    window.edit_cell(1, 8, "nonsense")
    assert errors and window.plan.tasks[0].mode == "units"


def test_summary_predecessors_are_editable_and_finish_links_are_refused(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=3), Task(2, "Phase"), Task(3, "Child", parent=2, duration=1)])
    window.reset_history(saved=True)
    window.refresh()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    assert window.can_edit_cell(window.plan.tasks[1], 5) and not window.can_edit_cell(window.plan.tasks[1], 2)
    window.edit_cell(2, 5, "1")
    assert window.plan.tasks[1].predecessors == [1]
    assert window.calculated[3].start.date().isoformat() == "2026-11-19"
    window.edit_cell(2, 5, "1FF")
    assert errors and "FS and SS" in errors[-1] and window.plan.tasks[1].predecessors == [1]


def test_task_dialog_offers_fixed_units_and_work_edit_changes_duration(app):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Staffed", mode="units", duration=2, resource="Alex")])
    dialog = TaskDialog(plan.tasks[0], plan)
    assert [dialog.mode.itemData(i) for i in range(dialog.mode.count())] == ["duration", "work", "units"]
    assert dialog.mode.currentData() == "units" and dialog.work.value() == 16
    dialog.work.setValue(40)
    dialog.apply(plan.tasks[0], plan)
    assert plan.tasks[0].mode == "units" and plan.tasks[0].duration == 5
    # Editing both fields keeps the duration that was typed.
    again = TaskDialog(plan.tasks[0], plan)
    again.duration.setValue(2)
    again.work.setValue(80)
    again.apply(plan.tasks[0], plan)
    assert plan.tasks[0].duration == 2


def test_task_dialog_fixes_a_start_and_a_blank_date_holds_the_calculated_start(app):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Lead in", duration=3),
                                           Task(2, "Follow-on", duration=2, predecessors=[1])])
    dialog = TaskDialog(plan.tasks[1], plan)
    assert [dialog.start_mode.itemData(i) for i in range(dialog.start_mode.count())] == ["no_earlier", "fixed"]
    assert dialog.start_mode.currentData() == "no_earlier"
    dialog.start_mode.setCurrentIndex(dialog.start_mode.findData("fixed"))
    dialog.apply(plan.tasks[1], plan)
    # With no date typed, fixing the start holds the date the task has now.
    assert (plan.tasks[1].start_mode, plan.tasks[1].earliest) == ("fixed", "2026-11-19")
    again = TaskDialog(plan.tasks[1], plan)
    assert again.start_mode.currentData() == "fixed" and again.earliest.text() == "2026-11-19"
    again.earliest.setText("2026-11-17")
    again.apply(plan.tasks[1], plan)
    assert schedule(plan)[2].start.date().isoformat() == "2026-11-17"


def test_start_cell_keeps_a_fixed_start_and_clearing_it_releases_it(app, monkeypatch):
    window = MainWindow()
    window.plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=3),
        Task(2, "Follow-on", duration=2, predecessors=[1], earliest="2026-11-17", start_mode="fixed")])
    window.refresh()
    assert window.calculated[2].start.date().isoformat() == "2026-11-17"
    assert set(window.fixed_conflicts) == {2}
    tip = window.items[2].toolTip(3)
    assert "Fixed start" in tip and "2026-11-19" in tip
    assert "Edit the earliest allowed start" in window.items[1].toolTip(3)   # an ordinary task is unchanged
    window.edit_cell(2, 3, "2026-11-18")                       # moving the date keeps the start fixed
    assert window.plan.tasks[1].start_mode == "fixed" and window.calculated[2].start.date().isoformat() == "2026-11-18"
    window.edit_cell(2, 3, "")                                 # clearing it releases the fixed start
    assert (window.plan.tasks[1].earliest, window.plan.tasks[1].start_mode) == ("", "no_earlier")
    assert window.calculated[2].start.date().isoformat() == "2026-11-19"
    assert not window.fixed_conflicts
    assert "Fixed start" not in window.items[2].toolTip(3)
