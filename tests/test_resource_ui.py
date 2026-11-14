import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from onepyproject.app import MainWindow, TaskDialog
from onepyproject.model import Plan, Resource, ResourceAssignment, Task, load_plan, schedule
from onepyproject.resource_ui import AssignResourcesDialog, ResourceSheetDialog
from onepyproject.resources import format_task_resources
from onepyproject.ui_preferences import DENSITY_HEIGHTS


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_resource_sheet_shows_overallocation_and_updates_capacity(app, monkeypatch):
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada", 100)], tasks=[
        Task(1, "A", assignments=[ResourceAssignment(1, 75)]),
        Task(2, "B", assignments=[ResourceAssignment(1, 75)]),
    ])
    dialog = ResourceSheetDialog(plan)
    assert dialog.table.item(0, 3).text() == "Overallocated"
    assert "150% assigned / 100% available" in dialog.table.item(0, 3).toolTip()
    dialog.table.item(0, 2).setText("200")
    assert dialog.table.item(0, 3).text() == "OK"
    dialog.table.item(0, 1).setText("Alice")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_resources == [Resource(1, "Alice", 200)]
    assert plan.resources[0].name == "Ada"  # edits remain a draft until accepted by the app


def test_resource_sheet_hides_row_numbers_but_keeps_stable_ids(app):
    plan = Plan(resources=[Resource(7, "Ada"), Resource(12, "Ben")])
    dialog = ResourceSheetDialog(plan)
    dialog.show()
    app.processEvents()
    assert dialog.table.verticalHeader().isHidden()
    assert dialog.table.horizontalHeaderItem(0).text() == "ID"
    assert [dialog.table.item(row, 0).text() for row in range(2)] == ["7", "12"]
    dialog.table.setCurrentCell(0, 1)
    dialog.remove_resource()
    assert dialog.table.item(0, 0).text() == "12"
    dialog.close()


def test_resource_dialogs_follow_layout_density(app):
    plan = Plan(resources=[Resource(1, "Ada")],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1)])])
    comfortable = ResourceSheetDialog(plan, density="comfortable")
    tight = ResourceSheetDialog(plan, density="tight")
    dense = ResourceSheetDialog(plan, density="dense")
    assert comfortable.table.rowHeight(0) > tight.table.rowHeight(0) > dense.table.rowHeight(0)
    assert comfortable.table.rowHeight(0) == DENSITY_HEIGHTS["comfortable"]
    assert tight.table.rowHeight(0) == DENSITY_HEIGHTS["tight"]
    assert dense.table.rowHeight(0) == DENSITY_HEIGHTS["dense"]
    assignments = AssignResourcesDialog(plan, plan.tasks[0], density="dense")
    assignments.show()
    app.processEvents()
    assert assignments.table.rowHeight(0) == DENSITY_HEIGHTS["dense"]
    assert assignments.rows[0][2].height() <= DENSITY_HEIGHTS["dense"]
    assignments.close()


def test_mainwindow_passes_density_to_resource_dialogs(app, tmp_path, monkeypatch):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(resources=[Resource(1, "Ada")],
                       tasks=[Task(1, "Task", assignments=[ResourceAssignment(1)])])
    window.reset_history(saved=True)
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    window.density_selector.setCurrentIndex(window.density_selector.findData("dense"))
    seen = []
    def reject(dialog):
        seen.append(dialog.table.rowHeight(0))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(ResourceSheetDialog, "exec", reject)
    monkeypatch.setattr(AssignResourcesDialog, "exec", reject)
    window.manage_resources()
    window.assign_resources()
    assert seen == [DENSITY_HEIGHTS["dense"], DENSITY_HEIGHTS["dense"]]
    assert not window.dirty and not window._undo


def test_resource_sheet_protects_assigned_resources_and_validates_names(app, monkeypatch):
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[Task(1, "Task", assignments=[ResourceAssignment(1)])])
    dialog = ResourceSheetDialog(plan)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    dialog.table.setCurrentCell(0, 1)
    dialog.remove_resource()
    assert dialog.table.rowCount() == 1
    assert "Remove this resource" in messages[-1]
    dialog.table.item(0, 1).setText("")
    assert dialog.table.item(0, 3).text() == "Check details"
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "non-empty" in messages[-1]


def test_resource_warning_uses_display_task_number(app, monkeypatch):
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[Task(40, "First"),
                Task(99, "Assigned", assignments=[ResourceAssignment(1)])])
    dialog = ResourceSheetDialog(plan)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    dialog.table.setCurrentCell(0, 1)
    dialog.remove_resource()
    assert "Assigned task IDs: 2" in messages[-1]
    assert dialog.table.rowCount() == 1


def test_assign_dialog_supports_multiple_percentages_and_fixed_work_guard(app, monkeypatch):
    plan = Plan(resources=[Resource(1, "Ada"), Resource(2, "Ben")], tasks=[
        Task(1, "Task", mode="work", work=80, assignments=[ResourceAssignment(1, 100)])])
    dialog = AssignResourcesDialog(plan, plan.tasks[0])
    dialog.rows[0][1].setCheckState(Qt.CheckState.Unchecked)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and messages
    dialog.rows[0][1].setCheckState(Qt.CheckState.Checked)
    dialog.rows[0][2].setValue(50)
    dialog.rows[1][1].setCheckState(Qt.CheckState.Checked)
    dialog.rows[1][2].setValue(125)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_assignments() == [ResourceAssignment(1, 50), ResourceAssignment(2, 125)]


def test_resource_dialogs_do_not_round_untouched_percentages(app):
    allocation = 37.12345678912345
    capacity = 100.12345678912345
    plan = Plan(resources=[Resource(1, "Ada", capacity)],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1, allocation)])])
    sheet = ResourceSheetDialog(plan)
    sheet.accept()
    assert sheet.result_resources[0].max_units == capacity
    assignments = AssignResourcesDialog(plan, plan.tasks[0])
    assignments.accept()
    assert assignments.selected_assignments()[0].units == allocation
    assert str(allocation) in assignments.rows[0][2].toolTip()


def test_small_allocation_is_visible_without_rounding_to_one_hundredth(app):
    plan = Plan(resources=[Resource(1, "Ada")],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1, 0.001)])])
    dialog = AssignResourcesDialog(plan, plan.tasks[0])
    assert dialog.rows[0][2].value() == 0.001
    assert dialog.selected_assignments()[0].units == 0.001


def test_mainwindow_resource_workflow_and_undo(app, tmp_path, monkeypatch):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Build", duration=5), Task(2, "Other", duration=5)])
    window.reset_history(saved=True)
    window.refresh()
    window.tree.setCurrentItem(window.items[1], 1)
    def add_resources(dialog):
        dialog._append(Resource(1, "Ada", 100))
        dialog._append(Resource(2, "Ben", 100))
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(ResourceSheetDialog, "exec", add_resources)
    window.manage_resources()
    assert [(resource.uid, resource.name) for resource in window.plan.resources] == [(1, "Ada"), (2, "Ben")]
    def assign(dialog):
        dialog.rows[0][1].setCheckState(Qt.CheckState.Checked)
        dialog.rows[0][2].setValue(50)
        dialog.rows[1][1].setCheckState(Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(AssignResourcesDialog, "exec", assign)
    window.assign_resources()
    assert window.plan.tasks[0].assignments == [ResourceAssignment(1, 50), ResourceAssignment(2, 100)]
    assert window.calculated[1].work == 60
    assert window.items[1].text(6) == "Ada[50%], Ben"
    original_schedule = schedule(window.plan)
    window.edit_cell(2, 6, "Ada[80%]")
    assert "1 overallocated resource(s)" in window.statusBar().currentMessage()
    assert window.calculated[1] == original_schedule[1]  # warning does not level tasks
    assert "overallocated" in window.items[1].toolTip(6)
    window.undo()
    assert not window.plan.tasks[1].assignments
    window.redo()
    assert window.plan.tasks[1].assignments == [ResourceAssignment(1, 80)]
    def rename(dialog):
        dialog.table.item(0, 1).setText("Alice")
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(ResourceSheetDialog, "exec", rename)
    window.manage_resources()
    assert window.plan.resources[0].name == "Alice"
    assert window.items[1].text(6) == "Alice[50%], Ben"
    path = tmp_path / "resources.pxplan"
    window.save_onepyproject(str(path))
    restored = load_plan(path)
    assert restored.resources == window.plan.resources
    assert restored.tasks[0].assignments == window.plan.tasks[0].assignments


def test_inline_resources_rollback_and_task_dialog_preserves_multiple(app, tmp_path, monkeypatch):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(tasks=[Task(1, "Task", duration=2)])
    window.reset_history(saved=True)
    window.refresh()
    window.edit_cell(1, 6, "Ada[50%],Ben")
    assert window.items[1].text(6) == "Ada[50%], Ben"
    assert window.calculated[1].work == 24
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.edit_cell(1, 6, "New[50%],Ben[-1%]")
    assert errors
    assert [resource.name for resource in window.plan.resources] == ["Ada", "Ben"]
    assert window.items[1].text(6) == "Ada[50%], Ben"
    dialog = TaskDialog(window.plan.tasks[0], window.plan)
    dialog.name.setText("Renamed task")
    dialog.apply(window.plan.tasks[0], window.plan)
    assert format_task_resources(window.plan, window.plan.tasks[0]) == "Ada[50%], Ben"


def test_task_dialog_preserves_effort_and_duration_when_switching_modes(app):
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada"), Resource(2, "Ben")], tasks=[
        Task(1, "Task", duration=5, assignments=[ResourceAssignment(1, 50), ResourceAssignment(2, 100)]),
    ])
    task = plan.tasks[0]
    dialog = TaskDialog(task, plan)
    assert dialog.work.value() == 60
    dialog.mode.setCurrentIndex(dialog.mode.findData("work"))
    dialog.apply(task, plan)
    assert task.mode == "work" and task.work == 60
    assert schedule(plan)[1].duration == 5
    task.work = 80.123456789
    dialog = TaskDialog(task, plan)
    dialog.name.setText("New name")
    dialog.apply(task, plan)
    assert task.work == 80.123456789
    before = schedule(plan)[1].duration
    dialog = TaskDialog(task, plan)
    dialog.mode.setCurrentIndex(dialog.mode.findData("duration"))
    dialog.apply(task, plan)
    assert task.mode == "duration" and task.duration == before
