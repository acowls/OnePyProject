"""Qt desktop interface for the experimental OnePyProject planner."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from html import escape
import math
from pathlib import Path
import re
import sys
import unicodedata
import zlib

from PySide6.QtCore import QDate, QEvent, QLocale, QSize, QTime, Qt, QRectF, QTimer, QItemSelectionModel, QSettings
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
                               QFrame, QGroupBox, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStyledItemDelegate, QTabWidget, QToolBar, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import QPointF

from . import __version__
from .model import (MODE_LABELS, MODE_NAMES, MODES, START_MODE_LABELS, START_MODES, Plan, Task,
                    completion_percentages, descendants, estimated_tasks, indent, load_plan, outdent, save_plan,
                    schedule, start_conflicts, total_units)
from .dependencies import parse_dependency, parse_display_predecessors, remap_predecessors, task_numbers
from .icons import application_icon, toolbar_icon
from .mpp import EXPORT_WARNING, IMPORT_WARNING, export_mpp, import_mpp
from .resources import format_task_resources, migrate_legacy_resources, overallocation_warnings, set_task_resources
from .workcalendar import format_period, parse_periods
from .resource_ui import AssignResourcesDialog, ResourceSheetDialog
from .resource_completion import ResourceCompleter
from .report import export_html
from .report_pdf import export_pdf
from .ui_preferences import DEFAULT_DENSITY, DENSITY_HEIGHTS, DENSITY_PREFERENCE_KEY, read_density_preference
from .preferences import application_preferences
from .clipboard import clear_cut, get_rows, paste_rows, put_rows, release_rows
from .ai.proposals import ProposalError, plan_revision
from .ai.preview import CHANGED_CELL, ROW_COLORS, build_preview
from .ai.ui import AIAssistantDock


DATE_FORMATS = ("system", "short", "long")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
EMPTY_ROW_HINT = "Enter/dbl-click adds; Shift+Enter inserts"
TASK_COLUMN_KEYS = ("id", "task", "duration", "start", "finish", "predecessors", "resource", "work", "mode", "complete")
COLUMN_WIDTH_KEY = "display/task_column_widths"
OPTIONAL_COLUMNS = (("finish", "Finish"), ("predecessors", "Predecessors"), ("resource", "Resource"),
                    ("work", "Work"), ("mode", "Mode"), ("complete", "Complete"))
SUMMARY_PALETTE = (
    "#c9def2", "#9fc8e7", "#76add8", "#4c91c5",
    "#cce8d6", "#a4d5b6", "#77bf91", "#4a9e6a",
    "#ded3f0", "#bca8df", "#987bc9", "#7555ae",
    "#f4dfb8", "#e9c982", "#d9aa51", "#bf8730",
)
SUMMARY_PALETTE_NAMES = (
    "Blue mist", "Sky blue", "Lake blue", "Deep blue",
    "Mint mist", "Soft green", "Leaf green", "Forest green",
    "Lilac mist", "Soft violet", "Amethyst", "Deep violet",
    "Cream", "Soft gold", "Amber", "Deep amber",
)


def summary_color(name: str) -> str:
    """Stable 16-color assignment; phonetic first-word groups stay adjacent."""
    normalized = unicodedata.normalize("NFKD", name.casefold()).encode("ascii", "ignore").decode("ascii")
    words = [part for part in re.findall(r"[a-z]+", normalized)
             if part not in {"phase", "stage", "task", "item", "complete", "completed"}]
    if not words:
        words = ["summary"]

    def soundex(word):
        codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
                 **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}
        first = word[0]
        encoded = []
        previous = codes.get(first, "")
        for character in word[1:]:
            current = codes.get(character, "")
            if current and current != previous:
                encoded.append(current)
            previous = current
        return first + "".join(encoded[:3]).ljust(3, "0")

    hue_group = sum(ord(character) for character in soundex(words[0])) % 4
    shade_seed = " ".join(sorted(words[1:] or words))
    shade = zlib.crc32(shade_seed.encode("utf-8")) % 4
    return SUMMARY_PALETTE[hue_group * 4 + shade]


def collapsed_summary_color(name: str, automatic: bool, custom: str = "") -> str:
    return custom or (summary_color(name) if automatic else "#72a9e0")


def display_time(value: datetime, date_format: str = "system", include_time: bool = True) -> str:
    if date_format == "short":
        text = f"{value.day:02d} {MONTH_NAMES[value.month - 1]}"
    elif date_format == "long":
        text = value.strftime("%Y-%m-%d")
    else:
        text = QLocale.system().toString(QDate(value.year, value.month, value.day), QLocale.FormatType.ShortFormat)
    if not include_time:
        return text
    if date_format == "system":
        locale = QLocale.system()
        clock = QTime(value.hour, value.minute, value.second)
        style = QLocale.FormatType.LongFormat if value.second or value.microsecond else QLocale.FormatType.ShortFormat
        clock_text = locale.toString(clock, style)
        if value.microsecond:
            clock_text += f" (+0.{value.microsecond:06d}s)"
    else:
        clock_text = value.strftime("%H:%M")
        if value.second or value.microsecond:
            clock_text += value.strftime(":%S")
            if value.microsecond:
                clock_text += f".{value.microsecond:06d}".rstrip("0")
    return f"{text} {clock_text}"


class TaskTree(QTreeWidget):
    """Keyboard shortcuts apply to rows, not to text being edited."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.placeholder = None
        self.insertion = None
        self.insertion_before = None
        self.duration_editor = None

    def event(self, event):
        if self.window.ai_preview is not None and event.type() == QEvent.Type.KeyPress:
            bar = self.verticalScrollBar()
            key = event.key()
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                delta = bar.pageStep() if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown) else bar.singleStep()
                bar.setValue(bar.value() + (-delta if key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp) else delta))
            return True
        if event.type() == QEvent.Type.KeyPress and self.state() != QAbstractItemView.State.EditingState:
            for shortcut, handler in ((QKeySequence.StandardKey.Cut, self.window.cut_rows),
                                      (QKeySequence.StandardKey.Copy, self.window.copy_rows),
                                      (QKeySequence.StandardKey.Paste, self.window.paste_rows)):
                if event.matches(shortcut):
                    handler()
                    return True
            key = event.key()
            modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if (self.window.digit_edits_duration and modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier)
                    and Qt.Key.Key_0 <= key <= Qt.Key.Key_9 and event.text() in "0123456789"
                    and len(event.text()) == 1 and self.window.selected_uid() is not None):
                self.edit_duration_from_digit(event.text())
                return True
            if modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    if key == Qt.Key.Key_Backtab or modifiers == Qt.KeyboardModifier.ShiftModifier:
                        self.window.outdent_task()
                    else:
                        self.window.indent_task()
                    return True
                if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and modifiers == Qt.KeyboardModifier.NoModifier:
                    self.window.delete_task()
                    return True
                if (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.ShiftModifier
                        and self.window.selected_uid() is not None):
                    self.insert_above_current()
                    return True
                if modifiers == Qt.KeyboardModifier.NoModifier:
                    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentItem() in (None, self.placeholder, self.insertion):
                        item = self.insertion if self.currentItem() is self.insertion and self.insertion else self.placeholder
                        if item is not None:
                            self.setCurrentItem(item, 1)
                            self.editItem(item, 1)
                        return True
                    if key == Qt.Key.Key_Insert and self.window.selected_uid() is not None:
                        self.insert_above_current()
                        return True
        return super().event(event)

    def edit_duration_from_digit(self, digit):
        uid = self.window.selected_uid()
        task = next((task for task in self.window.plan.tasks if task.uid == uid), None)
        if task is None:
            return
        if not self.window.can_edit_cell(task, 2):
            self.window.statusBar().showMessage("Duration is calculated for summary or fixed-work tasks.", 5000)
            return
        node = self.window.items[uid]
        self.duration_editor = None
        self.setCurrentItem(node, 2)
        self.editItem(node, 2)
        editor = self.duration_editor
        self.duration_editor = None
        if editor is not None:
            editor.setText(digit)
            editor.setCursorPosition(len(digit))
            editor.setFocus()

    def viewportEvent(self, event):
        if self.window.ai_preview is not None and event.type() in (QEvent.Type.MouseButtonPress,
                                                                    QEvent.Type.MouseButtonDblClick):
            return True
        if (event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton
                and self.state() != QAbstractItemView.State.EditingState):
            self.select_context_item(event.position().toPoint())
            return True
        if (event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.state() != QAbstractItemView.State.EditingState and self.placeholder is not None
                and self.itemAt(event.position().toPoint()) is None):
            self.setCurrentItem(self.placeholder, 1)
            return True
        if (event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton
                and self.state() != QAbstractItemView.State.EditingState and self.placeholder is not None):
            item = self.itemAt(event.position().toPoint())
            if item is None or item is self.placeholder:
                self.setCurrentItem(self.placeholder, 1)
                self.editItem(self.placeholder, 1)
                return True
        return super().viewportEvent(event)

    def select_context_item(self, position):
        self.select_context_node(self.itemAt(position))

    def select_context_node(self, item):
        if item is None or item in (self.placeholder, self.insertion):
            self.clearSelection()
            if self.placeholder is not None:
                self.setCurrentItem(self.placeholder, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            return
        if not item.isSelected():
            self.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        else:
            self.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)

    def contextMenuEvent(self, event):
        if self.window.ai_preview is not None or self.state() == QAbstractItemView.State.EditingState:
            return
        if event.reason() != event.Reason.Keyboard:
            self.select_context_item(event.pos())
        menu = self.window.task_context_menu(self)
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()

    def insert_above_current(self):
        uid = self.window.selected_uid()
        node = self.window.items.get(uid)
        if node is None or self.insertion is not None:
            return
        parent = node.parent()
        item = QTreeWidgetItem(["", "New task…"])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setForeground(1, QColor("#748399"))
        if parent:
            parent.insertChild(parent.indexOfChild(node), item)
        else:
            self.insertTopLevelItem(self.indexOfTopLevelItem(node), item)
        self.insertion, self.insertion_before = item, uid
        self.setCurrentItem(item, 1)
        self.editItem(item, 1)

    def editor_closed(self, *_):
        item = self.insertion
        if item is not None:
            QTimer.singleShot(0, lambda: self.remove_insertion(item))

    def remove_insertion(self, item):
        if self.insertion is not item:
            return
        was_current = self.currentItem() is item
        before = self.insertion_before
        parent = item.parent()
        if parent:
            parent.removeChild(item)
        else:
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))
        self.insertion = self.insertion_before = None
        if was_current and before in self.window.items:
            self.setCurrentItem(self.window.items[before], 1)


class TaskDelegate(QStyledItemDelegate):
    """Edit supported cells, while keeping calculated cells read-only."""

    def __init__(self, window):
        super().__init__(window.tree)
        self.window = window

    def eventFilter(self, editor, event):
        if isinstance(editor, QLineEdit) and isinstance(editor.completer(), ResourceCompleter):
            completer = editor.completer()
            if (event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)
                    and completer.popup().isVisible()
                    and completer.move_selection(1 if event.key() == Qt.Key.Key_Down else -1)):
                return True
            if (event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab)
                    and completer.popup().isVisible() and completer.accept_current()):
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape and completer.popup().isVisible():
                completer.dismiss()
                return True
            if event.type() == QEvent.Type.FocusOut and completer.accepting:
                return True
        return super().eventFilter(editor, event)

    def createEditor(self, parent, option, index):
        if self.window.ai_preview is not None:
            return None
        uid = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if uid is None and index.column() == 1:
            return QLineEdit(parent)
        task = next((t for t in self.window.plan.tasks if t.uid == uid), None)
        if task is None or not self.window.can_edit_cell(task, index.column()):
            return None
        if index.column() == 8:
            editor = QComboBox(parent)
            for mode in MODES:
                editor.addItem(MODE_LABELS[mode], mode)
            editor.setAccessibleName("Task scheduling mode")
            arrow = (Path(__file__).resolve().parent / "assets" / "ui" / "tree_open.svg").as_posix().replace('"', '\\"')
            # The native macOS combo is taller than dense rows and paints
            # transparently over the cell text. Use a compact, opaque editor.
            editor.setStyleSheet(f"""
                QComboBox {{ color: #24354a; background: #ffffff; border: 1px solid #5484b7;
                             border-radius: 2px; padding: 0 18px 0 3px; }}
                QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
                                        width: 16px; border: 0; }}
                QComboBox::down-arrow {{ image: url("{arrow}"); width: 10px; height: 10px; }}
                QComboBox QAbstractItemView {{ color: #24354a; background: #ffffff;
                                               selection-color: #183753; selection-background-color: #d5e9fa; }}
            """)
            def commit_selection(_index):
                self.commitData.emit(editor)
                self.closeEditor.emit(editor)
            editor.activated.connect(commit_selection)
            return editor
        if index.column() == 9:
            editor = QDoubleSpinBox(parent)
            editor.setRange(0, 100)
            editor.setDecimals(1)
            editor.setSuffix(" %")
            editor.setAccessibleName("Task percent complete")
            return editor
        editor = QLineEdit(parent)
        if index.column() == 2:
            self.window.tree.duration_editor = editor
        elif index.column() == 6:
            completer = ResourceCompleter(editor, [resource.name for resource in self.window.plan.resources])
            editor.setCompleter(completer)
            # QLineEdit normally previews a highlighted completion by replacing
            # the whole field and moving the cursor. That breaks token editing.
            # Our completer applies a choice only when it is accepted instead.
            QTimer.singleShot(0, completer.disable_inline_preview)
        return editor

    def updateEditorGeometry(self, editor, option, index):
        if index.column() == 9 and isinstance(editor, QDoubleSpinBox):
            cell = option.rect
            viewport = editor.parentWidget()
            width = min(viewport.width(), max(cell.width(), editor.sizeHint().width()))
            height = min(viewport.height(), max(cell.height(), editor.sizeHint().height()))
            x = max(0, min(cell.right() + 1 - width, viewport.width() - width))
            y = max(0, min(cell.y() + (cell.height() - height) // 2, viewport.height() - height))
            editor.setGeometry(x, y, width, height)
            return
        if index.column() != 8 or not isinstance(editor, QComboBox):
            return super().updateEditorGeometry(editor, option, index)
        # Temporarily expand a narrow cell so both the full label and arrow fit,
        # without changing the user's saved column width or row density.
        cell = option.rect
        viewport = editor.parentWidget()
        width = min(viewport.width(), max(cell.width(), editor.sizeHint().width()))
        height = min(viewport.height(), max(cell.height(), editor.sizeHint().height()))
        x = max(0, min(cell.right() + 1 - width, viewport.width() - width))
        y = max(0, min(cell.y() + (cell.height() - height) // 2, viewport.height() - height))
        editor.setGeometry(x, y, width, height)

    def setEditorData(self, editor, index):
        uid = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if uid is None:
            editor.clear()
            editor.setPlaceholderText("New task name")
            return
        task = next(t for t in self.window.plan.tasks if t.uid == uid)
        column = index.column()
        if column == 8:
            editor.setCurrentIndex(editor.findData(task.mode))
            return
        if column == 9:
            editor.setValue(task.percent_complete)
            return
        displayed = remap_predecessors(task.predecessors, task_numbers(self.window.plan.tasks))
        values = {1: task.name, 2: f"{task.duration}{'?' if task.estimated else ''}",
                  3: task.earliest or self.window.calculated[uid].start.date().isoformat(),
                  5: ", ".join(map(str, displayed)), 6: format_task_resources(self.window.plan, task), 7: str(task.work)}
        editor.setText(values[column])
        editor.selectAll()

    def setModelData(self, editor, model, index):
        uid = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        column = index.column()
        value = editor.currentData() if column == 8 else editor.value() if column == 9 else editor.text()
        current_plan = self.window.plan
        row = self.window.tree.itemFromIndex(index)
        before = self.window.tree.insertion_before if row is self.window.tree.insertion else None
        # Rebuilding a tree while Qt is still closing its editor is unsafe.
        def commit():
            if self.window.plan is not current_plan:
                return
            if uid is None:
                self.window.add_inline_task(value, before)
            else:
                self.window.edit_cell(uid, column, value)
        QTimer.singleShot(0, commit)


class TaskDialog(QDialog):
    def __init__(self, task: Task, plan: Plan, parent=None):
        super().__init__(parent)
        numbers = task_numbers(plan.tasks)
        self.setWindowTitle(f"Task {numbers.get(task.uid, len(plan.tasks) + 1)}")
        self.resize(860, 460)
        self.initial_mode = task.mode
        calculated = schedule(plan).get(task.uid) if any(other.uid == task.uid for other in plan.tasks) else None
        self.display_duration = calculated.duration if calculated and task.mode == "work" else task.duration
        has_resources = bool(task.resource.strip() or task.assignments)
        self.display_work = (calculated.work if calculated and task.mode in ("duration", "units") and has_resources
                             else task.work)
        self.duration_changed = False
        self.work_changed = False
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        layout.addLayout(form)
        self.name = QLineEdit(task.name)
        self.name.setMinimumWidth(500)
        self.calculated_start = calculated.start.date().isoformat() if calculated else plan.start
        self.earliest = QLineEdit(task.earliest)
        self.earliest.setPlaceholderText("YYYY-MM-DD; blank uses project start")
        self.start_mode = QComboBox()
        for start_mode in START_MODES:
            self.start_mode.addItem(START_MODE_LABELS[start_mode], start_mode)
        self.start_mode.setCurrentIndex(self.start_mode.findData(task.start_mode))
        self.start_mode.setAccessibleName("Start rule")
        self.start_mode.setToolTip("No earlier than: predecessors can still move the task later. "
                                   "Fixed: the task stays on this date whatever its predecessors say, "
                                   "like a task that has already started.")
        self.mode = QComboBox()
        for mode in MODES:
            self.mode.addItem(MODE_NAMES[mode], mode)
        self.mode.setCurrentIndex(self.mode.findData(task.mode))
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0, 100000)
        self.duration.setDecimals(2)
        self.duration.setValue(self.display_duration)
        self.duration.setSuffix(" days")
        self.duration.setMaximumWidth(260)
        self.duration.valueChanged.connect(lambda _value: setattr(self, "duration_changed", True))
        self.estimated = QCheckBox("Estimated")
        self.estimated.setAccessibleName("Estimated duration")
        self.estimated.setToolTip("Mark this duration as estimated; estimated values show a ?")
        self.estimated.setChecked(task.estimated)
        self.duration_row = QWidget()
        duration_layout = QHBoxLayout(self.duration_row)
        duration_layout.setContentsMargins(0, 0, 0, 0)
        duration_layout.addWidget(self.duration)
        duration_layout.addWidget(self.estimated)
        duration_layout.addStretch()
        self.work = QDoubleSpinBox()
        self.work.setRange(0, 800000)
        self.work.setDecimals(2)
        self.work.setValue(self.display_work)
        self.work.setSuffix(" hours")
        self.work.valueChanged.connect(lambda _value: setattr(self, "work_changed", True))
        self.resource = QLineEdit(format_task_resources(plan, task))
        self.resource.setMinimumWidth(500)
        self.resource.setPlaceholderText("Ada[50%], Ben")
        self.summary = any(other.parent == task.uid for other in plan.tasks)
        self.summary_color_value = task.summary_color
        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(2)
        self.summary_color_buttons = {}
        self.default_color_button = self._make_summary_color_button(
            "", "Default / Auto (blue bar)", "A")
        color_layout.addWidget(self.default_color_button)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        color_layout.addWidget(divider)
        for name, color in zip(SUMMARY_PALETTE_NAMES, SUMMARY_PALETTE):
            button = self._make_summary_color_button(color, name)
            color_layout.addWidget(button)
        self.custom_color_button = QPushButton("Custom")
        self.custom_color_button.setAccessibleName("Choose custom summary color")
        self.custom_color_button.setToolTip("Choose any custom color")
        self.custom_color_button.clicked.connect(self.choose_summary_color)
        color_layout.addWidget(self.custom_color_button)
        self._update_summary_color_buttons()
        self.summary_color_row = color_row
        color_layout.addStretch()
        self.percent_complete = QDoubleSpinBox()
        self.percent_complete.setRange(0, 100)
        self.percent_complete.setDecimals(1)
        self.percent_complete.setSuffix(" %")
        self.percent_complete.setValue(completion_percentages(plan).get(task.uid, task.percent_complete))
        self.percent_changed = False
        self.percent_complete.valueChanged.connect(lambda _value: setattr(self, "percent_changed", True))
        self.percent_complete.setEnabled(not self.summary)
        if self.summary:
            self.percent_complete.setToolTip("Summary completion is calculated from its subtasks.")
        if self.summary:
            self.resource.setReadOnly(True)
            self.resource.setToolTip("Summary assignments cannot be added; use Assign to remove existing ones.")
        self.original_predecessors = ", ".join(map(str, remap_predecessors(task.predecessors, numbers)))
        self.predecessors = QLineEdit(self.original_predecessors)
        self.predecessors.setMinimumWidth(500)
        self.predecessors.setPlaceholderText("e.g. 5FS+2d, 6SS-1d")
        for label, widget in (("Name", self.name), ("Start date", self.earliest), ("Start rule", self.start_mode),
                              ("Scheduling", self.mode), ("Duration", self.duration_row),
                              ("Complete", self.percent_complete), ("Work", self.work), ("Resources", self.resource),
                              ("Predecessors", self.predecessors)):
            form.addRow(label, widget)
        if self.summary:
            form.addRow("Summary bar", color_row)
        hint = QLabel("A duration or work of 0 creates a milestone. Summary dates are calculated from children. "
                      "Fixed units: edit duration or work and the other is recalculated; if you edit both, duration wins.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self, task: Task, plan: Plan) -> None:
        task.name = self.name.text().strip()
        task.earliest = self.earliest.text().strip()
        task.start_mode = self.start_mode.currentData()
        if task.start_mode == "fixed" and not task.earliest:
            # Fixing a start holds the date the task has now.
            task.earliest = self.calculated_start
        task.mode = self.mode.currentData()
        if self.duration_changed:
            task.duration = self.duration.value()
        elif self.initial_mode == "work" and task.mode != "work":
            task.duration = self.display_duration
        task.estimated = self.estimated.isChecked()
        if not self.summary and self.percent_changed:
            task.percent_complete = self.percent_complete.value()
        if self.work_changed:
            task.work = self.work.value()
        elif self.initial_mode != "work" and task.mode == "work":
            task.work = self.display_work
        if not self.summary:
            set_task_resources(plan, task, self.resource.text())
        else:
            task.summary_color = self.summary_color_value
        if task.mode == "units" and self.work_changed and not self.duration_changed and total_units(task) > 0:
            # Fixed units: units stay put, so editing only the work changes the duration.
            days = task.work / (8 * total_units(task) / 100)
            if not math.isfinite(days) or days > 100000:
                raise ValueError("Duration cannot exceed 100,000 days")
            task.duration = days
        if self.predecessors.text() != self.original_predecessors:
            task.predecessors = parse_display_predecessors(self.predecessors.text(), plan.tasks)

    def _make_summary_color_button(self, color, label, text=""):
        button = QPushButton(text)
        button.setFixedSize(20, 20)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.clicked.connect(lambda checked=False, value=color: self.set_summary_color(value))
        if color:
            self.summary_color_buttons[color] = button
        else:
            self.default_color_button = button
        return button

    def _update_summary_color_buttons(self):
        for color, button in self.summary_color_buttons.items():
            chosen = color.casefold() == self.summary_color_value.casefold()
            button.setStyleSheet(
                f"QPushButton {{ background: {color}; color: #17212b; border: "
                f"{3 if chosen else 1}px solid {'#102b43' if chosen else '#8799a8'}; border-radius: 3px; }}")
        is_default = not self.summary_color_value
        self.default_color_button.setStyleSheet(
            f"QPushButton {{ background: #72a9e0; color: #102b43; border: "
            f"{3 if is_default else 1}px solid {'#102b43' if is_default else '#8799a8'}; border-radius: 3px; font-weight: bold; }}")
        is_custom = bool(self.summary_color_value) and self.summary_color_value.casefold() not in {
            color.casefold() for color in self.summary_color_buttons}
        self.custom_color_button.setStyleSheet(
            "QPushButton { background: #edf3f7; color: #17334a; "
            f"border: {2 if is_custom else 1}px solid {'#102b43' if is_custom else '#a3b3bf'}; border-radius: 3px; padding: 2px 6px; }}")

    def set_summary_color(self, color):
        self.summary_color_value = color
        self._update_summary_color_buttons()

    def choose_summary_color(self):
        initial = QColor(self.summary_color_value or summary_color(self.name.text()))
        chosen = QColorDialog.getColor(initial, self, "Choose collapsed summary color")
        if chosen.isValid():
            self.set_summary_color(chosen.name())


class SettingsDialog(QDialog):
    def __init__(self, plan: Plan, parent=None, digit_edits_duration=True, date_format="system",
                 show_icon_labels=False, settings_location="", confirm_task_deletion=True, density=DEFAULT_DENSITY,
                 visible_columns=None, color_collapsed_summaries=True):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(700, 600)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabWidget::pane { background: palette(window); }")
        project_tab = QWidget()
        project_layout = QVBoxLayout(project_tab)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.title = QLineEdit(plan.title)
        self.title.setMinimumWidth(400)
        self.start = QLineEdit(plan.start)
        self.holidays = QPlainTextEdit("\n".join(plan.holidays))
        self.holidays.setAccessibleName("Project holidays")
        self.holidays.setPlaceholderText("One date or period per line, with an optional name:\n"
                                         "2026-12-25 Christmas Day\n2026-12-21..2027-01-01 Christmas shutdown")
        self.holidays.setMinimumHeight(110)
        self.working_days = QPlainTextEdit("\n".join(plan.working_days))
        self.working_days.setAccessibleName("Extra working days")
        self.working_days.setPlaceholderText("Dates worked even though they fall on a weekend, e.g. 2026-10-31 Catch-up Saturday")
        self.working_days.setMinimumHeight(70)
        self.working_days.setMaximumHeight(100)
        form.addRow("Project name", self.title)
        form.addRow("Start date", self.start)
        form.addRow("Holidays", self.holidays)
        form.addRow("Extra working days", self.working_days)
        project_layout.addLayout(form)
        hint = QLabel("Working time: Monday–Friday, 08:00–12:00 and 13:00–17:00. Holidays are not worked; extra working "
                      "days are worked on the same hours even on a weekend. Enter one date (YYYY-MM-DD) or period "
                      "(YYYY-MM-DD..YYYY-MM-DD) per line or separated by commas, optionally followed by a name "
                      "(names cannot contain commas). Periods must not overlap.")
        hint.setWordWrap(True)
        project_layout.addWidget(hint)
        project_layout.addStretch()
        self.tabs.addTab(project_tab, "Project-specific settings")
        preferences_tab = QWidget()
        preferences_layout = QVBoxLayout(preferences_tab)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.digit_edits_duration = QCheckBox("Type digits on a task to edit its duration")
        self.digit_edits_duration.setChecked(digit_edits_duration)
        form.addRow("Keyboard", self.digit_edits_duration)
        self.confirm_task_deletion = QCheckBox("Confirm before deleting a task")
        self.confirm_task_deletion.setChecked(confirm_task_deletion)
        self.confirm_task_deletion.setToolTip("Deleting a summary also deletes its subtasks and their dependency links. Undo can restore them.")
        form.addRow("Deletion", self.confirm_task_deletion)
        self.show_icon_labels = QCheckBox("Show toolbar icon names")
        self.show_icon_labels.setChecked(show_icon_labels)
        form.addRow("Toolbar", self.show_icon_labels)
        self.date_format = QComboBox()
        self.date_format.addItem("System (default)", "system")
        self.date_format.addItem("11 Nov HH:MM", "short")
        self.date_format.addItem("YYYY-MM-DD HH:MM", "long")
        self.date_format.setCurrentIndex(max(0, self.date_format.findData(date_format)))
        form.addRow("Date format", self.date_format)
        self.density_selector = QComboBox()
        self.density_selector.setAccessibleName("Layout density")
        self.density_selector.setToolTip("Choose the compactness of task and resource rows")
        for name in DENSITY_HEIGHTS:
            self.density_selector.addItem(name.capitalize(), name)
        self.density_selector.setCurrentIndex(max(0, self.density_selector.findData(density)))
        form.addRow("Layout", self.density_selector)
        self.color_collapsed_summaries = QCheckBox("Automatically color collapsed summary bars")
        self.color_collapsed_summaries.setChecked(color_collapsed_summaries)
        self.color_collapsed_summaries.setToolTip("Similar task names use related colors. Expanded summaries remain black.")
        form.addRow("Gantt", self.color_collapsed_summaries)
        self.settings_location = QLineEdit(settings_location)
        self.settings_location.setReadOnly(True)
        self.settings_location.setCursorPosition(0)
        self.settings_location.setToolTip("Application preferences are stored separately from the project file.\n" + settings_location)
        form.addRow("Preferences location", self.settings_location)
        preferences_layout.addLayout(form)
        visibility = QGroupBox("Optional task columns")
        visibility_grid = QGridLayout(visibility)
        visible_columns = visible_columns or {}
        self.column_visibility = {}
        for index, (key, label) in enumerate(OPTIONAL_COLUMNS):
            checkbox = QCheckBox(label)
            checkbox.setChecked(visible_columns.get(key, True))
            self.column_visibility[key] = checkbox
            visibility_grid.addWidget(checkbox, index // 3, index % 3)
        preferences_layout.addWidget(visibility)
        preferences_layout.addStretch()
        self.tabs.addTab(preferences_tab, "User preferences")
        layout.addWidget(self.tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ExportDialog(QFileDialog):
    """Choose the destination, export type, and options in one window."""

    FILTERS = {
        "mpp": "Microsoft Project (*.mpp)",
        "html": "Web page (*.html)",
        "pdf": "PDF (*.pdf)",
    }

    def __init__(self, parent=None, initial_format="mpp", initial_path=None, template_hint=None, protected_paths=()):
        super().__init__(parent)
        self.setWindowTitle("Export project")
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        self.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        self.setFileMode(QFileDialog.FileMode.AnyFile)
        self.setNameFilters(list(self.FILTERS.values()))
        self.setLabelText(QFileDialog.DialogLabel.FileType, "Export type:")
        self.setLabelText(QFileDialog.DialogLabel.Accept, "Export")
        self.resize(900, 700)
        self.output_path = None
        self.template_path = None
        self.template_hint = str(template_hint or "")
        self.protected_paths = tuple(Path(path) for path in protected_paths if path is not None)

        # Qt's non-native file dialog exposes its controls and permits the
        # additional options needed alongside its standard file browser.
        grid = self.layout()
        type_combo = self.findChild(QComboBox, "fileTypeCombo")
        self.filename_edit = self.findChild(QLineEdit, "fileNameEdit")
        button_box = self.findChild(QDialogButtonBox, "buttonBox")
        if not isinstance(grid, QGridLayout) or type_combo is None or self.filename_edit is None or button_box is None:
            raise RuntimeError("Cannot initialize the export file dialog")
        self._last_format = None
        type_combo.currentIndexChanged.connect(self.update_options)
        grid.removeWidget(button_box)
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.template_label = QLabel("MPP template")
        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText("Choose a user-supplied MPP template")
        self.template_browse = QPushButton("Browse…")
        self.template_browse.clicked.connect(self.browse_template)
        self.template_row = QWidget()
        template_layout = QHBoxLayout(self.template_row)
        template_layout.setContentsMargins(0, 0, 0, 0)
        template_layout.addWidget(self.template_edit, 1)
        template_layout.addWidget(self.template_browse)
        form.addRow(self.template_label, self.template_row)
        self.paper_label = QLabel("Paper size")
        self.paper = QComboBox()
        self.paper.addItem("A4 landscape", "A4")
        self.paper.addItem("A3 landscape", "A3")
        form.addRow(self.paper_label, self.paper)
        self.show_time = QCheckBox("Show times (HH:MM)")
        form.addRow("", self.show_time)
        layout.addLayout(form)
        self.mpp_warning = QLabel(EXPORT_WARNING)
        self.mpp_warning.setWordWrap(True)
        self.mpp_warning.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.mpp_warning.setStyleSheet("QLabel { color: #704b12; background: #fff2d6; padding: 10px; }")
        layout.addWidget(self.mpp_warning)
        note = QLabel("Exports are copies of the current plan, including unsaved changes.")
        note.setWordWrap(True)
        layout.addWidget(note)
        grid.addWidget(panel, 4, 0, 1, 3)
        button_box.setOrientation(Qt.Orientation.Horizontal)
        grid.addWidget(button_box, 5, 0, 1, 3)
        self.set_format(initial_format)
        if initial_path:
            initial_path = Path(initial_path).expanduser()
            self.setDirectory(str(initial_path.parent))
            self.selectFile(initial_path.name)
            self.filename_edit.setText(initial_path.name)
        self.update_options()

    def selected_format(self):
        return next((name for name, name_filter in self.FILTERS.items()
                     if name_filter == self.selectedNameFilter()), "mpp")

    def set_format(self, name):
        self.selectNameFilter(self.FILTERS[name])
        self.update_options()

    def update_options(self, *_):
        selected = self.selected_format()
        if self._last_format and selected != self._last_format:
            name = self.filename_edit.text().strip()
            if name and Path(name).suffix.lower() in (".mpp", ".html", ".pdf"):
                target = Path(name).expanduser().with_suffix(f".{selected}")
                if target.is_absolute():
                    self.setDirectory(str(target.parent))
                    self.filename_edit.setText(target.name)
                else:
                    self.filename_edit.setText(str(target))
        self._last_format = selected
        self.setDefaultSuffix(selected)
        self.mpp_warning.setVisible(selected == "mpp")
        self.template_label.setVisible(selected == "mpp")
        self.template_row.setVisible(selected == "mpp")
        self.show_time.setVisible(selected in ("html", "pdf"))
        self.paper_label.setVisible(selected == "pdf")
        self.paper.setVisible(selected == "pdf")

    def browse_template(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select MPP template", self.template_edit.text() or self.template_hint,
            "Microsoft Project (*.mpp *.MPP)")
        if path:
            self.template_edit.setText(path)

    def accept(self):
        try:
            self._accept_selection()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot export", str(exc))

    def _accept_selection(self):
        name = self.filename_edit.text().strip()
        if not name:
            return
        typed_path = Path(name).expanduser()
        if not typed_path.is_absolute():
            typed_path = Path(self.directory().absolutePath()) / typed_path
        if typed_path.is_dir():
            self.setDirectory(str(typed_path))
            self.filename_edit.clear()
            return
        selected = self.selected_format()
        target = typed_path
        if target.suffix.lower() == f".{selected}":
            pass
        elif target.suffix.lower() in (".mpp", ".html", ".pdf"):
            target = target.with_suffix(f".{selected}")
        else:
            target = target.with_name(target.name + f".{selected}")
        if not target.parent.is_dir():
            QMessageBox.warning(self, "Cannot export", "Choose an existing destination folder.")
            return
        template_text = self.template_edit.text().strip()
        template = Path(template_text).expanduser() if template_text else None
        if template is not None and not template.is_absolute():
            template = Path(self.directory().absolutePath()) / template
        if selected == "mpp" and (template is None or not template.is_file()):
            QMessageBox.warning(self, "MPP template required", "Choose an existing user-supplied MPP template.")
            return
        for source in (*self.protected_paths, *((template,) if template else ())):
            if target.resolve() == source.resolve() or (target.exists() and source.exists() and target.samefile(source)):
                QMessageBox.warning(self, "Cannot export", "Cannot overwrite the current project or MPP template.")
                return
        if target.exists() or target.is_symlink():
            if selected == "mpp":
                QMessageBox.warning(self, "Cannot export", "Choose a new file name; MPP export does not overwrite existing files.")
                return
            if QMessageBox.question(self, "Replace existing file?", f"Replace {target.name}?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        # The filename edit and current directory are the source of truth. The
        # QFileDialog selection model may not be populated yet in offscreen or
        # freshly-opened dialogs, so do not let it veto a validated target.
        QDialog.accept(self)
        if self.result() == QDialog.DialogCode.Accepted:
            self.output_path = target
            self.template_path = template


class CalendarScrollArea(QScrollArea):
    def __init__(self, window):
        super().__init__()
        self.window = window

    def resizeEvent(self, event):
        super().resizeEvent(event)
        window = getattr(self, "window", None)
        if window is not None and hasattr(window, "gantt"):
            window.gantt.sync()


class Gantt(QWidget):
    DAY = 26

    def __init__(self, window: MainWindow):
        super().__init__()
        self.window = window
        self.origin = date.today()
        self.days = 90
        self.setMinimumHeight(150)

    def sync(self):
        values = list(self.window.calculated.values())
        if self.window.ai_preview is not None:
            values = [item for row in self.window.ai_preview.rows for item in (row.before, row.after) if item]
        if values:
            self.origin = min(v.start.date() for v in values) - timedelta(days=7)
            finish = max(v.finish.date() for v in values) + timedelta(days=21)
            self.days = max(90, (finish - self.origin).days + 1)
        else:
            self.origin = date.fromisoformat(self.window.plan.start) - timedelta(days=7)
            self.days = 90
        # Fill the viewport even when zoomed out on a short project.
        viewport_width = self.window.chart_area.viewport().width()
        self.days = max(self.days, math.ceil(viewport_width / self.DAY) + 14)
        # Preview must span both schedules, even when the proposal moves a date
        # beyond the normal chart horizon. Stay within Qt's widget-size limit.
        limit = 16_000_000 // self.DAY if self.window.ai_preview is not None else 5000
        self.days = min(self.days, limit)
        self.resize(self.days * self.DAY, max(150, self.window.tree.height()))
        self.update()

    def x(self, point: datetime) -> float:
        return ((point.date() - self.origin).days + (point.hour * 60 + point.minute) / 1440) * self.DAY

    def date_label(self, day: date) -> str:
        if self.DAY >= 9 and day.weekday() == 0:
            return day.strftime("%d %b")
        if 6 <= self.DAY < 9 and day.weekday() == 0 and day.isocalendar().week % 2 == 0:
            return day.strftime("%d %b")
        if self.DAY < 6 and day.day == 1:
            return day.strftime("%b %Y")
        return ""

    def contextMenuEvent(self, event):
        tree = self.window.tree
        if self.window.ai_preview is not None or tree.state() == QAbstractItemView.State.EditingState:
            return
        if event.reason() != event.Reason.Keyboard:
            y = event.pos().y() - tree.header().height()
            if y < 0 or y >= tree.viewport().height():
                return
            item = next((node for node in self.window.items.values()
                         if not tree.visualItemRect(node).isEmpty()
                         and tree.visualItemRect(node).top() <= y <= tree.visualItemRect(node).bottom()), None)
            tree.select_context_node(item)
        menu = self.window.task_context_menu(tree)
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(event.rect(), QColor("#ffffff"))
        tree = self.window.tree
        header = tree.header().height()
        preview = self.window.ai_preview
        if preview is not None:
            for row in preview.rows:
                rect = tree.visualItemRect(self.window.items[row.uid])
                if not rect.isEmpty() and row.status in ROW_COLORS:
                    p.fillRect(0, header + rect.top(), self.width(), rect.height(), QColor(ROW_COLORS[row.status]))
        p.fillRect(0, 0, self.width(), header, QColor("#f4f6f9"))
        clip = event.rect()
        first = max(0, clip.left() // self.DAY)
        last = min(self.days, clip.right() // self.DAY + 2)
        for n in range(first, last):
            day = self.origin + timedelta(days=n)
            x = n * self.DAY
            if day.weekday() >= 5 and preview is None:
                p.fillRect(x, header, self.DAY, self.height() - header, QColor("#f6f7f9"))
            p.setPen(QPen(QColor("#e9edf2"), 1))
            p.drawLine(x, header, x, self.height())
            label = self.date_label(day)
            if label:
                p.setPen(QColor("#68788c"))
                p.drawText(x + 3, 19, label)
        # Rows may have negative viewport coordinates when the tree scrolls.
        # Clip all bars and links below the fixed date header.
        p.save()
        p.setClipRect(QRectF(0, header, self.width(), max(0, self.height() - header)), Qt.ClipOperation.IntersectClip)
        today = (date.today() - self.origin).days * self.DAY
        p.setPen(QPen(QColor("#55a378"), 1, Qt.PenStyle.DashLine))
        p.drawLine(today, header, today, self.height())

        positions = {}
        preview_rows = {row.uid: row for row in preview.rows} if preview else {}
        progress_values = preview.after_progress if preview else self.window.progress_percentages
        for uid, tree_item in self.window.items.items():
            rect = tree.visualItemRect(tree_item)
            if rect.isEmpty():
                continue
            y = header + rect.center().y()
            row = preview_rows.get(uid)
            item = (row.before or row.after) if row else self.window.calculated[uid]
            start, finish = self.x(item.start), self.x(item.finish)
            positions[uid] = (start, finish, y)
            p.setPen(QPen(QColor("#e9edf2"), 1))
            p.drawLine(0, header + rect.bottom(), self.width(), header + rect.bottom())
            if row is not None:
                self.draw_preview_bar(p, item, y, proposed=row.before is None)
                base_progress = row.before_progress if row.before is not None else row.after_progress
                self.draw_completion(p, item, y, base_progress or 0, summary=item.summary and not tree_item.isExpanded())
                if row.before and row.after and (row.before.start != row.after.start or row.before.finish != row.after.finish):
                    self.draw_preview_bar(p, row.after, y, proposed=True)
            elif item.summary:
                task = next(task for task in self.window.plan.tasks if task.uid == uid)
                self.draw_summary_bar(p, item, y, task.name, tree_item.isExpanded(),
                                      progress_values.get(uid, 0), task.summary_color)
            elif item.duration == 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#344d69"))
                p.drawPolygon(QPolygonF([QPointF(start, y - 6), QPointF(start + 6, y),
                                         QPointF(start, y + 6), QPointF(start - 6, y)]))
                self.draw_completion(p, item, y, progress_values.get(uid, 0))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#72a9e0"))
                p.drawRoundedRect(QRectF(start, y - 6, max(5, finish - start), 12), 2, 2)
                self.draw_completion(p, item, y, progress_values.get(uid, 0))
        # Draw FS links for visible rows, without affecting bar hit testing.
        p.setPen(QPen(QColor("#5484b7"), 1))
        for task in (() if preview else self.window.plan.tasks):
            if task.uid not in positions:
                continue
            sx, sf, sy = positions[task.uid]
            for pred in task.predecessors:
                link = parse_dependency(pred)
                if link.uid not in positions:
                    continue
                ps, pf, py = positions[link.uid]
                px = pf if link.kind in ("FS", "FF") else ps
                endpoint = sx if link.kind in ("FS", "SS") else sf
                elbow = max(px + 7, endpoint - 8)
                p.drawLine(int(px), int(py), int(elbow), int(py))
                p.drawLine(int(elbow), int(py), int(elbow), int(sy))
                p.drawLine(int(elbow), int(sy), int(endpoint), int(sy))
        p.restore()
        p.end()

    def draw_summary_bar(self, painter, item, y, name, expanded, progress, custom_color=""):
        start, finish = self.x(item.start), self.x(item.finish)
        if expanded:
            painter.setPen(QPen(QColor("#343c49"), 3))
            painter.drawLine(int(start), int(y - 3), int(max(start + 2, finish)), int(y - 3))
            painter.drawLine(int(start), int(y - 3), int(start), int(y + 4))
            painter.drawLine(int(finish), int(y - 3), int(finish), int(y + 4))
        else:
            color = collapsed_summary_color(name, self.window.color_collapsed_summaries, custom_color)
            painter.setPen(QPen(QColor(color).darker(125), 1))
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(start, y - 6, max(5, finish - start), 12), 2, 2)
        self.draw_completion(painter, item, y, progress, summary=not expanded)

    def draw_completion(self, painter, item, y, percent, summary=False):
        if percent <= 0:
            return
        start, finish = self.x(item.start), self.x(item.finish)
        if item.duration == 0:
            if percent >= 100:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#17334a"))
                painter.drawPolygon(QPolygonF([QPointF(start, y - 3), QPointF(start + 3, y),
                                               QPointF(start, y + 3), QPointF(start - 3, y)]))
            return
        end = start + max(0, finish - start) * max(0, min(100, percent)) / 100
        if percent >= 100:
            end = finish
        painter.setPen(QPen(QColor("#17334a"), 4 if summary else 3, Qt.PenStyle.SolidLine))
        painter.drawLine(QPointF(start, y), QPointF(end, y))

    def draw_preview_bar(self, painter, item, y, proposed=False):
        start, finish = self.x(item.start), self.x(item.finish)
        painter.setBrush(QColor("#357ab8" if proposed else "#a9cdec"))
        painter.setPen(QPen(QColor("#18598f"), 1.5, Qt.PenStyle.DashLine) if proposed else Qt.PenStyle.NoPen)
        if item.duration == 0:
            painter.drawPolygon(QPolygonF([QPointF(start, y - 6), QPointF(start + 6, y),
                                           QPointF(start, y + 6), QPointF(start - 6, y)]))
        else:
            height = 8 if proposed else 12
            painter.drawRoundedRect(QRectF(start, y - height / 2, max(5, finish - start), height), 2, 2)


class MainWindow(QMainWindow):
    ZOOM_STEPS = (5, 8, 13, 18, 26)

    def __init__(self, preferences=None):
        super().__init__()
        self.preferences = preferences if preferences is not None else application_preferences()
        self.digit_edits_duration = self.preferences.value("keyboard/digit_edits_duration", True, type=bool)
        self.confirm_task_deletion = self.preferences.value("editing/confirm_task_deletion", True, type=bool)
        self.color_collapsed_summaries = self.preferences.value("display/color_collapsed_summaries", True, type=bool)
        self.visible_task_columns = {
            key: self.preferences.value(f"display/task_column_visibility/{key}", True, type=bool)
            for key, _label in OPTIONAL_COLUMNS
        }
        self.show_icon_labels = self.preferences.value("display/show_icon_labels", False, type=bool)
        self.date_format = self.preferences.value("display/date_format", "system", type=str)
        if self.date_format not in DATE_FORMATS:
            self.date_format = "system"
        self.density = read_density_preference(self.preferences)
        self.plan = Plan()
        self.calculated = {}
        self.items: dict[int, QTreeWidgetItem] = {}
        self.plan_path: Path | None = None
        self.import_path: Path | None = None
        self.template = Path.cwd() / "template.mpp"
        self.dirty = False
        self._saved_plan: Plan | None = copy.deepcopy(self.plan)
        self._undo: list[tuple[Plan, list[int]]] = []
        self._redo: list[tuple[Plan, list[int]]] = []
        self._ai_dialog = None
        self.ai_preview = None
        self._preview_state = None
        saved_zoom = self.preferences.value("display/zoom_width", 26, type=int)
        self.zoom_index = self.ZOOM_STEPS.index(saved_zoom) if saved_zoom in self.ZOOM_STEPS else self.ZOOM_STEPS.index(26)
        self.setWindowTitle(f"OnePyProject {__version__}")
        self.setWindowIcon(application_icon())
        self.resize(1450, 800)

        self.file_toolbar = QToolBar("File")
        self.task_toolbar = QToolBar("Project")
        for toolbar in (self.file_toolbar, self.task_toolbar):
            toolbar.setMovable(False)
            toolbar.setIconSize(QSize(20, 20))
            toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        file_actions = (("New", self.new_plan, "new"), ("Open", self.open_plan, "open"),
                        ("Save", self.save, "save"), ("Save As", self.save_as, "save_as"),
                        ("Import", self.open_mpp, "import"), ("Export", self.export_project, "export"))
        task_actions = (("Settings", self.settings, "settings"), ("Resources", self.manage_resources, "resources"),
                        ("Assign", self.assign_resources, "assign"), ("+ Task", self.add_task, "task"), ("Edit", self.edit_task, "edit"),
                        ("Delete", self.delete_task, "delete"), ("Undo", self.undo, "undo"),
                        ("Redo", self.redo, "redo"), ("Link", self.link_tasks, "link"),
                        ("Unlink", self.unlink_tasks, "unlink"), ("Indent", self.indent_task, "indent"),
                        ("Outdent", self.outdent_task, "outdent"))
        for toolbar, entries in ((self.file_toolbar, file_actions), (self.task_toolbar, task_actions)):
            for title, handler, icon in entries:
                action = QAction(toolbar_icon(icon), title, self)
                if handler is not None:
                    action.triggered.connect(handler)
                action.setToolTip(title)
                if title == "Undo":
                    self.undo_action = action
                    action.setShortcuts(QKeySequence.StandardKey.Undo)
                    self.addAction(action)
                elif title == "Redo":
                    self.redo_action = action
                    action.setShortcuts(QKeySequence.StandardKey.Redo)
                    self.addAction(action)
                elif title == "Import":
                    action.setToolTip("Import an MPP file")
                elif title == "Export":
                    action.setToolTip("Export an MPP, HTML or PDF copy")
                toolbar.addAction(action)
                toolbar.widgetForAction(action).setAccessibleName(title)
        self.apply_toolbar_labels()
        self.control_tabs = QTabWidget()
        self.control_tabs.setDocumentMode(True)
        self.control_tabs.addTab(self.file_toolbar, "File")
        self.control_tabs.addTab(self.task_toolbar, "Project")
        # Project actions are the normal working view; File remains the first tab.
        self.control_tabs.setCurrentIndex(1)
        zoom_style = """
            QToolButton { color: #17334a; background: #edf3f7; border: 1px solid #a3b3bf;
                          border-radius: 4px; padding: 2px 6px; font-size: 16px; font-weight: bold; }
            QToolButton:hover { background: #cfe2f1; }
            QToolButton:disabled { color: #51616f; background: #dce4e9; }
        """
        self.zoom_out_button = QToolButton()
        self.zoom_out_button.setText("−")
        self.zoom_out_button.setStyleSheet(zoom_style)
        self.zoom_out_button.setToolTip("Zoom out: show more dates")
        self.zoom_out_button.setAccessibleName("Zoom out")
        self.zoom_out_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_out_button.clicked.connect(self.zoom_out)
        self.zoom_indicator = QLabel("100%")
        self.zoom_indicator.setStyleSheet("color: #17334a; background: #edf3f7; border-radius: 3px; padding: 3px 5px;")
        self.zoom_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in_button = QToolButton()
        self.zoom_in_button.setText("+")
        self.zoom_in_button.setStyleSheet(zoom_style)
        self.zoom_in_button.setToolTip("Zoom in: show fewer dates")
        self.zoom_in_button.setAccessibleName("Zoom in")
        self.zoom_in_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_in_button.clicked.connect(self.zoom_in)
        self.ai_button = QToolButton()
        icon_path = Path(__file__).resolve().parent / "assets" / "ui" / "ai_assistant.svg"
        self.ai_button.setIcon(QIcon(str(icon_path)))
        self.ai_button.setIconSize(QSize(20, 20))
        self.ai_button.setStyleSheet(zoom_style)
        self.ai_button.setToolTip("Open or hide the AI assistant")
        self.ai_button.setAccessibleName("AI assistant")
        self.ai_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.ai_button.clicked.connect(self.open_ai)
        self.statusBar().addPermanentWidget(self.zoom_out_button)
        self.statusBar().addPermanentWidget(self.zoom_indicator)
        self.statusBar().addPermanentWidget(self.zoom_in_button)
        self.statusBar().addPermanentWidget(self.ai_button)
        splitter = QSplitter()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.control_tabs)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.tree = TaskTree(self)
        self.cut_action = QAction(QIcon(str(Path(__file__).resolve().parent / "assets" / "ui" / "cut.svg")), "Cut", self)
        self.copy_action = QAction(QIcon(str(Path(__file__).resolve().parent / "assets" / "ui" / "copy.svg")), "Copy", self)
        self.paste_action = QAction(QIcon(str(Path(__file__).resolve().parent / "assets" / "ui" / "paste.svg")), "Paste", self)
        for action, shortcut in ((self.cut_action, QKeySequence.StandardKey.Cut),
                                 (self.copy_action, QKeySequence.StandardKey.Copy),
                                 (self.paste_action, QKeySequence.StandardKey.Paste)):
            action.setShortcuts(shortcut)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            self.tree.viewport().addAction(action)
        self.tree.setColumnCount(10)
        self.tree.setHeaderLabels(["ID", "Task", "Duration", "Start", "Finish", "Predecessors", "Resource", "Work", "Mode", "Complete"])
        self.tree.setTreePosition(1)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(20)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        delegate = TaskDelegate(self)
        delegate.closeEditor.connect(self.tree.editor_closed)
        self.tree.setItemDelegate(delegate)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setMaximumSectionSize(10000)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header_metrics = header.fontMetrics()
        bold_font = QFont(self.tree.font())
        bold_font.setBold(True)
        data_metrics = QFontMetrics(bold_font)
        duration_width = header_metrics.horizontalAdvance("Duration") + 14
        date_width = max(data_metrics.horizontalAdvance("2020-08-08"), header_metrics.horizontalAdvance("Finish")) + 12
        work_width = max(data_metrics.horizontalAdvance("888 h"), header_metrics.horizontalAdvance("Work")) + 12
        for i, width in enumerate((48, 250, duration_width, date_width, date_width, 130, 110, work_width, 90, 82)):
            value = self.preferences.value(f"{COLUMN_WIDTH_KEY}/{TASK_COLUMN_KEYS[i]}", None)
            if isinstance(value, int) and not isinstance(value, bool):
                saved_width = value
            elif isinstance(value, str) and len(value) <= 5 and value.isascii() and value.isdecimal():
                saved_width = int(value)
            else:
                saved_width = 0
            if header.minimumSectionSize() <= saved_width <= header.maximumSectionSize():
                width = saved_width
            self.tree.setColumnWidth(i, width)
        self.apply_column_visibility()
        splitter.addWidget(self.tree)
        self.chart_area = CalendarScrollArea(self)
        self.chart_area.setWidgetResizable(False)
        self.chart_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gantt = Gantt(self)
        self.gantt.setToolTip("A dark line through a task bar shows percent complete. Collapsed summary bars can be colored from the Project toolbar.")
        self.gantt.DAY = self.ZOOM_STEPS[self.zoom_index]
        self.chart_area.setWidget(self.gantt)
        splitter.addWidget(self.chart_area)
        splitter.setSizes([820, 630])
        self.tree.verticalScrollBar().valueChanged.connect(self.gantt.update)
        self.tree.itemExpanded.connect(lambda *_: self.gantt.update())
        self.tree.itemCollapsed.connect(lambda *_: self.gantt.update())
        self.setStyleSheet("""
            QMainWindow { background: #f5f7fa; }
            QTabWidget::pane { border: 0; background: #f5f7fa; }
            QTabBar::tab { color: #425a70; background: #e4ebf1; padding: 6px 18px; }
            QTabBar::tab:selected { color: #17344c; background: #f5f7fa; font-weight: bold; }
            QToolBar { spacing: 3px; padding: 7px; background: #f5f7fa; border: 0; }
            QToolButton { padding: 6px 5px; border-radius: 4px; color: #24354a; }
            QToolButton:hover { background: #dfeaf6; }
            QStatusBar { background: #253444; color: #f3f7fb; }
            QStatusBar::item { border: 0; }
            QTreeWidget { border: 0; background: white; color: #24354a; alternate-background-color: #f9fafc; }
            QTreeWidget::item { height: 29px; color: #24354a; }
            QTreeWidget::item:selected { background: #b9d8f2; color: #102b43; }
            QTreeWidget::item:focus { border: 0; outline: none; }
            QTreeWidget::branch:has-children:closed:selected,
            QTreeWidget::branch:has-children:open:selected { background: #b9d8f2; border: 0; }
            QTreeWidget::branch:has-children:closed:focus,
            QTreeWidget::branch:has-children:open:focus { background: #b9d8f2; border: 0; }
            QHeaderView::section { background: #f4f6f9; padding: 6px; border: 0; border-right: 1px solid #e3e7ed; color: #54657b; }
        """)
        self.zoom_indicator.setFixedWidth(self.zoom_indicator.sizeHint().width())
        self.apply_density()
        self.update_zoom_controls()
        self.refresh()
        self._column_width_sync = QTimer(self)
        self._column_width_sync.setSingleShot(True)
        self._column_width_sync.setInterval(300)
        self._column_width_sync.timeout.connect(self.sync_preferences)
        header.sectionResized.connect(self.save_column_width)
        if preferences is None and self.preferences.status() != QSettings.Status.NoError:
            QTimer.singleShot(0, self.sync_preferences)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "gantt"):
            self.gantt.sync()

    def apply_density(self):
        height = DENSITY_HEIGHTS[self.density]
        icon_dir = Path(__file__).resolve().parent / "assets" / "ui"
        closed_icon = (icon_dir / "tree_closed.svg").as_posix().replace('"', '\\"')
        open_icon = (icon_dir / "tree_open.svg").as_posix().replace('"', '\\"')
        self.tree.setStyleSheet(f"""
            QTreeWidget {{ border: 0; background: white; color: #24354a; alternate-background-color: #f9fafc; }}
            QTreeWidget::item {{ height: {height}px; color: #24354a; }}
            QTreeWidget::item:selected {{ background: #b9d8f2; color: #102b43; }}
            QTreeWidget::item:focus {{ border: 0; outline: none; }}
            QTreeWidget::branch:has-children:closed {{ image: url("{closed_icon}"); }}
            QTreeWidget::branch:has-children:open {{ image: url("{open_icon}"); }}
            QTreeWidget::branch:has-children:closed:selected,
            QTreeWidget::branch:has-children:open:selected {{ background: #b9d8f2; border: 0; }}
            QTreeWidget::branch:has-children:closed:focus,
            QTreeWidget::branch:has-children:open:focus {{ background: #b9d8f2; border: 0; }}
        """)
        self.tree.doItemsLayout()
        if hasattr(self, "gantt"):
            self.gantt.sync()

    def apply_column_visibility(self):
        for key, _label in OPTIONAL_COLUMNS:
            self.tree.setColumnHidden(TASK_COLUMN_KEYS.index(key), not self.visible_task_columns.get(key, True))

    def save_column_width(self, index, _old_width, new_width):
        if 0 <= index < len(TASK_COLUMN_KEYS):
            self.preferences.setValue(f"{COLUMN_WIDTH_KEY}/{TASK_COLUMN_KEYS[index]}", new_width)
            self._column_width_sync.start()

    def save_column_widths(self):
        for index, key in enumerate(TASK_COLUMN_KEYS):
            self.preferences.setValue(f"{COLUMN_WIDTH_KEY}/{key}", self.tree.columnWidth(index))

    def apply_toolbar_labels(self):
        style = (Qt.ToolButtonStyle.ToolButtonTextUnderIcon if self.show_icon_labels
                 else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.file_toolbar.setToolButtonStyle(style)
        self.task_toolbar.setToolButtonStyle(style)
        if self.show_icon_labels:
            self.file_toolbar.setIconSize(QSize(16, 16))
            self.task_toolbar.setIconSize(QSize(16, 16))
            self.file_toolbar.setStyleSheet("QToolButton { padding: 2px 2px; font-size: 9px; }")
            self.task_toolbar.setStyleSheet("QToolButton { padding: 2px 2px; font-size: 9px; }")
        else:
            self.file_toolbar.setIconSize(QSize(20, 20))
            self.task_toolbar.setIconSize(QSize(20, 20))
            self.file_toolbar.setStyleSheet("")
            self.task_toolbar.setStyleSheet("")

    def sync_preferences(self):
        self.preferences.sync()
        if self.preferences.status() != QSettings.Status.NoError:
            QMessageBox.warning(self, "Preferences not saved",
                                f"Could not save application preferences to {self.preferences.fileName()}.")

    def update_zoom_controls(self):
        self.zoom_out_button.setEnabled(self.zoom_index > 0)
        self.zoom_in_button.setEnabled(self.zoom_index < len(self.ZOOM_STEPS) - 1)
        self.zoom_indicator.setText(f"{round(self.ZOOM_STEPS[self.zoom_index] / 26 * 100)}%")

    def change_zoom(self, step):
        index = max(0, min(len(self.ZOOM_STEPS) - 1, self.zoom_index + step))
        if index == self.zoom_index:
            return
        bar = self.chart_area.horizontalScrollBar()
        viewport_width = self.chart_area.viewport().width()
        center_day = (bar.value() + viewport_width / 2) / self.gantt.DAY
        self.zoom_index = index
        self.gantt.DAY = self.ZOOM_STEPS[index]
        self.gantt.sync()
        bar.setValue(round(center_day * self.gantt.DAY - viewport_width / 2))
        self.preferences.setValue("display/zoom_width", self.gantt.DAY)
        self.sync_preferences()
        self.update_zoom_controls()

    def zoom_out(self):
        self.change_zoom(-1)

    def zoom_in(self):
        self.change_zoom(1)

    def selected_uid(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def selected_uids(self):
        selected = {item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()}
        return [task.uid for task in self.plan.tasks if task.uid in selected]

    def task_context_menu(self, parent=None):
        """Build actions for the current selection without changing toolbar state."""
        selected = self.selected_uids()
        selected_set = set(selected)
        source = self.plan
        revision = plan_revision(source)
        editable = self.ai_preview is None
        rows = get_rows() if editable else None
        can_paste = rows is not None
        if can_paste and rows.cut:
            anchor = selected[-1] if selected else None
            try:
                can_paste = (rows.source() is self and self.plan is rows.source_plan
                             and plan_revision(self.plan) == rows.source_revision
                             and anchor not in {task.uid for task in rows.tasks})
            except ProposalError:
                can_paste = False
        can_unlink = len(selected) >= 2 and any(
            parse_dependency(value).uid in selected_set
            for task in self.plan.tasks if task.uid in selected_set for value in task.predecessors)
        can_link = False
        if len(selected) >= 2 and all(not self.calculated[uid].summary for uid in selected):
            candidate = copy.deepcopy(self.plan)
            by_uid = {task.uid: task for task in candidate.tasks}
            for pred, succ in zip(selected, selected[1:]):
                if pred not in {parse_dependency(value).uid for value in by_uid[succ].predecessors}:
                    by_uid[succ].predecessors.append(pred)
                    can_link = True
            if can_link:
                try:
                    schedule(candidate)
                except (ValueError, KeyError, OverflowError, RecursionError):
                    can_link = False
        menu = QMenu(parent or self.tree)
        actions = (("Cut", self.cut_rows, self.cut_action.icon(), bool(selected)),
                   ("Copy", self.copy_rows, self.copy_action.icon(), bool(selected)),
                   ("Paste", self.paste_rows, self.paste_action.icon(), can_paste),
                   ("Edit", self.edit_task, toolbar_icon("edit"), len(selected) == 1),
                   ("Delete", self.delete_task, toolbar_icon("delete"), len(selected) == 1),
                   ("Unlink", self.unlink_tasks, toolbar_icon("unlink"), can_unlink),
                   ("Link", self.link_tasks, toolbar_icon("link"), can_link),
                   ("New task", self.add_task, toolbar_icon("task"), True))
        for label, handler, icon, enabled in actions:
            action = menu.addAction(icon, label)
            action.setEnabled(editable and enabled)
            def invoke(checked=False, callback=handler):
                if self.ai_preview is not None:
                    return
                if self.plan is not source or plan_revision(self.plan) != revision or self.selected_uids() != selected:
                    self.statusBar().showMessage("The project or selection changed; open the menu again.", 5000)
                    return
                callback()
            action.triggered.connect(invoke)
        return menu

    def copy_rows(self):
        if self.ai_preview is None and put_rows(self, self.selected_uids()):
            self.statusBar().showMessage("Rows copied. Paste after a selected task or at the end.", 5000)

    def cut_rows(self):
        if self.ai_preview is None and put_rows(self, self.selected_uids(), cut=True):
            self.statusBar().showMessage("Rows ready to move. Choose a destination and paste; the originals remain until then.", 7000)

    def paste_rows(self):
        if self.ai_preview is not None:
            return
        rows = get_rows()
        if rows is None:
            self.statusBar().showMessage("No OnePyProject rows are available to paste.", 5000)
            return
        selected = self.selected_uids()
        anchor = selected[-1] if selected else None
        try:
            candidate, pasted, dropped = paste_rows(self, rows, anchor)
        except (ValueError, KeyError, OverflowError, RecursionError) as exc:
            QMessageBox.warning(self, "Cannot paste rows", str(exc))
            return
        def apply():
            self.plan.tasks = candidate.tasks
            self.plan.resources = candidate.resources
        if self.mutate(apply, pasted):
            clear_cut(rows)
            self.statusBar().showMessage(f"Rows pasted.{f' {dropped} unsupported predecessor link(s) omitted.' if dropped else ''}", 7000)

    def refresh(self, selected=None):
        expanded = {uid for uid, item in self.items.items() if item.isExpanded()}
        had_items = bool(self.items)
        previous = self.tree.verticalScrollBar().value()
        if selected is None:
            selection = self.selected_uids()
        else:
            selection = [selected] if isinstance(selected, int) else list(selected)
        self.calculated = schedule(self.plan)
        self.fixed_conflicts = start_conflicts(self.plan, self.calculated)
        progress = completion_percentages(self.plan, self.calculated)
        self.progress_percentages = progress
        self.resource_warnings = overallocation_warnings(self.plan)
        estimates = estimated_tasks(self.plan)
        numbers = task_numbers(self.plan.tasks)
        self.tree.insertion = self.tree.insertion_before = None
        self.tree.clear()
        self.items = {}
        for task in self.plan.tasks:
            item = self.calculated[task.uid]
            show_time = 0 < item.duration < 1
            start = display_time(item.start, self.date_format, include_time=show_time)
            finish = display_time(item.finish, self.date_format, include_time=show_time)
            values = [str(numbers[task.uid]), task.name, f"{item.duration:g} d{'?' if estimates[task.uid] else ''}",
                      start, finish,
                      ", ".join(map(str, remap_predecessors(task.predecessors, numbers))), format_task_resources(self.plan, task),
                      f"{item.work:g} h", MODE_LABELS[task.mode],
                      f"{progress[task.uid]:.1f}%"]
            node = QTreeWidgetItem(values)
            node.setData(0, Qt.ItemDataRole.UserRole, task.uid)
            node.setFlags(node.flags() | Qt.ItemFlag.ItemIsEditable)
            node.setToolTip(2, "A question mark indicates an estimated duration.")
            if task.start_mode == "fixed" and not item.summary:
                tip = (f"{display_time(item.start, self.date_format)}\nFixed start: predecessors do not move this task. "
                       "Edit the date (YYYY-MM-DD) to move it, or clear it to release the fixed start.")
                if task.uid in self.fixed_conflicts:
                    tip += (f"\nIts predecessors would start it on {self.fixed_conflicts[task.uid].date().isoformat()}; "
                            "the link is kept.")
                    node.setForeground(3, QColor("#a72a25"))
                font = QFont()
                font.setBold(True)
                node.setFont(3, font)
                node.setToolTip(3, tip)
            else:
                node.setToolTip(3, f"{display_time(item.start, self.date_format)}\nEdit the earliest allowed start (YYYY-MM-DD); predecessors may move it later.")
            node.setToolTip(4, display_time(item.finish, self.date_format))
            node.setToolTip(5, "Use displayed task IDs: 5, 5SS+3d, 5FF-1d, 5FS+2ed or 5FS+50%; separate with commas."
                               + (" A summary task supports only FS and SS links, which apply to every subtask."
                                  if item.summary else ""))
            node.setToolTip(8, "Duration: duration is entered, work is calculated. Work: work is entered, duration is "
                               "calculated. Units: either may be entered and the other is recalculated.")
            node.setToolTip(9, "Task completion is user-entered; summary completion is weighted by child work.")
            if item.summary and (task.assignments or task.resource.strip()):
                node.setToolTip(6, "Summary resources are retained but not included in scheduling or MPP export.")
            elif any(assignment.resource_uid in self.resource_warnings for assignment in task.assignments):
                node.setForeground(6, QColor("#a72a25"))
                node.setToolTip(6, "One or more resources are overallocated. Open the Resource sheet for details.")
            if item.summary:
                font = QFont()
                font.setBold(True)
                for col in range(10):
                    node.setFont(col, font)
            if task.parent:
                self.items[task.parent].addChild(node)
            else:
                self.tree.addTopLevelItem(node)
            self.items[task.uid] = node
        # The hint is longer than the Task column, so let its otherwise-empty
        # row span the grid instead of truncating the keyboard shortcut.
        space_width = max(1, self.tree.fontMetrics().horizontalAdvance(" "))
        indent = " " * ((self.tree.columnWidth(0) + self.tree.indentation()) // space_width)
        self.tree.placeholder = QTreeWidgetItem([indent + EMPTY_ROW_HINT, ""])
        self.tree.placeholder.setFlags(self.tree.placeholder.flags() | Qt.ItemFlag.ItemIsEditable)
        self.tree.placeholder.setForeground(0, QColor("#748399"))
        self.tree.placeholder.setToolTip(0, "Shift+Enter inserts a task above the selected task.")
        self.tree.addTopLevelItem(self.tree.placeholder)
        self.tree.placeholder.setFirstColumnSpanned(True)
        # Expand by default; retain a user's collapsed rows on subsequent refreshes.
        for uid, node in self.items.items():
            node.setExpanded(uid in expanded or not had_items)
        for uid in selection:
            if uid not in self.items:
                continue
            node = self.items[uid]
            parent = node.parent()
            while parent:
                parent.setExpanded(True)
                parent = parent.parent()
            node.setSelected(True)
        if selection and selection[-1] in self.items:
            index = self.tree.indexFromItem(self.items[selection[-1]])
            self.tree.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.tree.verticalScrollBar().setValue(previous)
        self.gantt.sync()
        self.setWindowTitle(f"{self.plan.title}{' *' if self.dirty else ''} — OnePyProject {__version__}")
        overload = f"  •  {len(self.resource_warnings)} overallocated resource(s)" if self.resource_warnings else ""
        self.statusBar().showMessage(f"{len(self.plan.tasks)} tasks  •  Standard calendar, 8h/day{overload}  •  MPP support is experimental")
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))

    def mutate(self, callback, selected=None):
        if self.ai_preview is not None:
            return False
        old = copy.deepcopy(self.plan)
        old_selection = self.selected_uids()
        try:
            callback()
            schedule(self.plan)
            if self.plan == old:
                return True
            self._undo.append((old, old_selection))
            self._undo = self._undo[-100:]
            self._redo.clear()
            self.dirty = self._saved_plan is None or self.plan != self._saved_plan
            self.refresh(selected)
            return True
        except (ValueError, KeyError, StopIteration, OverflowError) as exc:
            self.plan = old
            QMessageBox.warning(self, "Cannot update project", str(exc))
            return False

    def open_ai(self):
        if self._ai_dialog is None:
            self._ai_dialog = AIAssistantDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._ai_dialog)
            self.resizeDocks([self._ai_dialog], [360], Qt.Orientation.Horizontal)
        elif not self._ai_dialog.isHidden():
            self._ai_dialog.hide()
            return
        self._ai_dialog.show()
        self._ai_dialog.raise_()

    def show_ai_preview(self, before, after):
        preview = build_preview(before, after, lambda value, include_time=True:
                                display_time(value, self.date_format, include_time=include_time))
        if self.ai_preview is None:
            self._preview_state = {
                "expanded": {uid for uid, item in self.items.items() if item.isExpanded()},
                "selection": self.selected_uids(), "vertical": self.tree.verticalScrollBar().value(),
                "horizontal": self.chart_area.horizontalScrollBar().value(),
                "tree_horizontal": self.tree.horizontalScrollBar().value(),
                "actions": {action: action.isEnabled() for action in self.task_toolbar.actions()},
            }
        previous_preview = self.ai_preview
        self.ai_preview = preview
        self.file_toolbar.setEnabled(False)
        for action in self.task_toolbar.actions():
            action.setEnabled(False)
        self.zoom_out_button.setEnabled(False)
        self.zoom_in_button.setEnabled(False)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setItemsExpandable(False)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        try:
            self._render_ai_preview()
        except Exception:
            if previous_preview is None:
                self.clear_ai_preview()
            else:
                self.ai_preview = previous_preview
                self._render_ai_preview()
            raise

    def _render_ai_preview(self):
        previous = self.tree.verticalScrollBar().value()
        self.tree.clear()
        self.tree.placeholder = self.tree.insertion = self.tree.insertion_before = None
        self.items = {}
        for row in self.ai_preview.rows:
            node = QTreeWidgetItem(list(row.values))
            node.setData(0, Qt.ItemDataRole.UserRole, row.uid)
            node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsEditable)
            for col in range(10):
                color = CHANGED_CELL if row.status == "changed" and col in row.changed_columns else ROW_COLORS.get(row.status)
                if color:
                    node.setBackground(col, QColor(color))
                if row.previous is not None and col in row.changed_columns:
                    node.setToolTip(col, "<p>Current: " + escape(row.previous[col]) + "<br>Proposed: " +
                                    escape(row.values[col]) + "</p>")
            if row.details:
                node.setToolTip(1, row.details)
            item = row.after or row.before
            if item and item.summary:
                font = QFont()
                font.setBold(True)
                for col in range(10):
                    node.setFont(col, font)
            if row.parent:
                self.items[row.parent].addChild(node)
            else:
                self.tree.addTopLevelItem(node)
            self.items[row.uid] = node
            node.setExpanded(True)
        self.tree.verticalScrollBar().setValue(previous)
        self.gantt.sync()
        self.statusBar().showMessage("AI preview — read-only. Green: added; red: removed; yellow: changed.")

    def clear_ai_preview(self):
        if self.ai_preview is None:
            return
        state = self._preview_state
        self.ai_preview = None
        self._preview_state = None
        self.file_toolbar.setEnabled(True)
        for action, enabled in state["actions"].items():
            action.setEnabled(enabled)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setItemsExpandable(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.refresh(state["selection"])
        for uid, node in self.items.items():
            node.setExpanded(uid in state["expanded"])
        self.tree.verticalScrollBar().setValue(state["vertical"])
        self.tree.horizontalScrollBar().setValue(state["tree_horizontal"])
        self.chart_area.horizontalScrollBar().setValue(state["horizontal"])
        self.update_zoom_controls()
        self.gantt.update()

    def apply_ai_candidate(self, source, expected_revision, candidate, candidate_revision):
        """Apply a reviewed candidate as one atomic undoable working-plan edit."""
        if self.ai_preview is not None:
            raise ProposalError("Close the preview before applying the proposal.")
        if self.plan is not source or plan_revision(self.plan) != expected_revision:
            raise ProposalError("The project changed; request a new proposal before applying.")
        if plan_revision(candidate) != candidate_revision:
            raise ProposalError("The proposal changed; request a new preview before applying.")
        updated = copy.deepcopy(candidate)
        schedule(updated)
        overallocation_warnings(updated)
        if updated == self.plan:
            raise ProposalError("The proposal no longer changes the project.")
        old_plan, old_undo, old_redo, old_dirty = self.plan, self._undo, self._redo, self.dirty
        selection = self.selected_uids()
        try:
            self.plan = updated
            self._undo = (old_undo + [(copy.deepcopy(old_plan), selection)])[-100:]
            self._redo = []
            self.dirty = self._saved_plan is None or self.plan != self._saved_plan
            self.refresh(selection)
        except Exception as exc:
            self.plan, self._undo, self._redo, self.dirty = old_plan, old_undo, old_redo, old_dirty
            try:
                self.refresh(selection)
            except Exception:
                pass
            raise ProposalError("Could not apply the proposal; the working plan was restored.") from exc

    def reset_history(self, saved):
        self._undo.clear()
        self._redo.clear()
        self._saved_plan = copy.deepcopy(self.plan) if saved else None
        self.dirty = not saved

    def undo(self):
        if self.ai_preview is not None or not self._undo:
            return
        self._redo.append((copy.deepcopy(self.plan), self.selected_uids()))
        self.plan, selected = self._undo.pop()
        self.dirty = self._saved_plan is None or self.plan != self._saved_plan
        self.refresh(selected)

    def redo(self):
        if self.ai_preview is not None or not self._redo:
            return
        self._undo.append((copy.deepcopy(self.plan), self.selected_uids()))
        self.plan, selected = self._redo.pop()
        self.dirty = self._saved_plan is None or self.plan != self._saved_plan
        self.refresh(selected)

    def can_edit_cell(self, task, column):
        if column in (1, 5):
            return True
        if self.calculated[task.uid].summary:
            return False
        if column == 9:
            return True
        if column == 2:
            return task.mode in ("duration", "units")
        if column == 7:
            # Fixed units can derive duration from work only when something is assigned.
            return task.mode == "work" or (task.mode == "units" and total_units(task) > 0)
        return column in (3, 6, 8)

    def edit_cell(self, uid, column, value):
        task = next((t for t in self.plan.tasks if t.uid == uid), None)
        if task is None or not self.can_edit_cell(task, column):
            return
        def update():
            text = str(value).strip()
            if column == 1:
                task.name = text
            elif column == 2:
                task.estimated = text.endswith("?")
                if task.estimated:
                    text = text[:-1].strip()
                text = text.lower().removesuffix("days").removesuffix("day").removesuffix("d").strip()
                task.duration = float(text)
                if task.duration > 100000:
                    raise ValueError("Duration cannot exceed 100,000 days")
            elif column == 3:
                # Don't add an unintended date constraint if the displayed start was unchanged.
                if not task.earliest and text == self.calculated[uid].start.date().isoformat():
                    return
                task.earliest = date.fromisoformat(text).isoformat() if text else ""
                if not task.earliest:
                    task.start_mode = "no_earlier"  # Clearing the date also releases a fixed start.
            elif column == 5:
                current = ", ".join(map(str, remap_predecessors(task.predecessors, task_numbers(self.plan.tasks))))
                if text == current.strip():
                    return
                task.predecessors = parse_display_predecessors(text, self.plan.tasks)
            elif column == 6:
                set_task_resources(self.plan, task, text)
            elif column == 7:
                work = float(text.removesuffix("hours").removesuffix("hour").removesuffix("h").strip())
                if work > 800000:
                    raise ValueError("Work cannot exceed 800,000 hours")
                if task.mode == "units":
                    # Fixed units: units stay put, so changing work changes the duration.
                    days = work / (8 * total_units(task) / 100)
                    if not math.isfinite(days) or days > 100000:
                        raise ValueError("Duration cannot exceed 100,000 days")
                    task.duration = days
                else:
                    task.work = work
            elif column == 8:
                if text not in MODES:
                    raise ValueError(f"Scheduling mode must be one of: {', '.join(MODES)}")
                if text == "work" and task.mode != "work":
                    if not (task.resource.strip() or task.assignments):
                        raise ValueError("Assign a resource before switching to fixed work")
                    task.work = self.calculated[uid].work
                elif text != "work" and task.mode == "work":
                    task.duration = self.calculated[uid].duration
                task.mode = text
            elif column == 9:
                task.percent_complete = float(text.removesuffix("%").strip())
                if not 0 <= task.percent_complete <= 100:
                    raise ValueError("Percent complete must be between 0 and 100")
        self.mutate(update, uid)

    def link_tasks(self):
        selected = self.selected_uids()
        if len(selected) < 2:
            QMessageBox.information(self, "Link tasks", "Select at least two tasks. They will be linked in their row order.")
            return
        def link():
            by_uid = {task.uid: task for task in self.plan.tasks}
            for pred, succ in zip(selected, selected[1:]):
                if pred not in {parse_dependency(link).uid for link in by_uid[succ].predecessors}:
                    by_uid[succ].predecessors.append(pred)
        self.mutate(link, selected)

    def unlink_tasks(self):
        selected = self.selected_uids()
        if len(selected) < 2:
            QMessageBox.information(self, "Unlink tasks", "Select at least two tasks to remove links between them.")
            return
        selected_set = set(selected)
        def unlink():
            for task in self.plan.tasks:
                if task.uid in selected_set:
                    task.predecessors = [link for link in task.predecessors
                                         if parse_dependency(link).uid not in selected_set]
        self.mutate(unlink, selected)

    def confirm_discard(self):
        if not self.dirty:
            return True
        return QMessageBox.question(self, "Unsaved changes", "Discard unsaved changes?",
                                    QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def closeEvent(self, event):
        if self.confirm_discard():
            if self._ai_dialog is not None:
                self._ai_dialog.cancel_request()
                self.clear_ai_preview()
                self._ai_dialog.close()
            if self._column_width_sync.isActive():
                self._column_width_sync.stop()
            self.save_column_widths()
            self.sync_preferences()
            event.accept()
        else:
            event.ignore()

    def new_plan(self):
        if self.confirm_discard():
            self.plan, self.plan_path, self.import_path, self.dirty = Plan(), None, None, False
            self.reset_history(saved=True)
            self.refresh()

    def open_plan(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "",
            "Project files (*.pxplan *.PXPLAN *.mpp *.MPP);;"
            "Microsoft Project (*.mpp *.MPP);;OnePyProject plan (*.pxplan *.PXPLAN)")
        if not path:
            return
        if Path(path).suffix.lower() == ".mpp":
            self.open_mpp_file(path)
            return
        try:
            plan = load_plan(path)
            self.plan, self.plan_path, self.import_path, self.dirty = plan, Path(path), None, False
            self.reset_history(saved=True)
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open plan", str(exc))

    def save(self):
        if not self.plan_path:
            self.save_as()
            return
        self.save_onepyproject(str(self.plan_path))

    def save_as(self):
        path, file_type = QFileDialog.getSaveFileName(
            self, "Save As", str(self.plan_path or ""),
            "OnePyProject plan (*.pxplan);;Microsoft Project (*.mpp)")
        if not path:
            return
        if file_type == "Microsoft Project (*.mpp)" or path.lower().endswith(".mpp"):
            self.show_export_dialog("mpp", path)
            return
        self.save_onepyproject(path)

    def save_onepyproject(self, path):
        if not path.lower().endswith(".pxplan"):
            path += ".pxplan"
        try:
            save_plan(self.plan, path)
            self.plan_path, self.dirty = Path(path), False
            self._saved_plan = copy.deepcopy(self.plan)
            self.refresh(self.selected_uid())
        except Exception as exc:
            QMessageBox.critical(self, "Cannot save plan", str(exc))

    def open_mpp(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open MPP14", "", "Microsoft Project (*.mpp *.MPP)")
        if not path:
            return
        self.open_mpp_file(path)

    def open_mpp_file(self, path):
        if QMessageBox.warning(self, "Lossy MPP import", IMPORT_WARNING,
                               QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
            return
        try:
            plan, notes = import_mpp(path)
            self.plan, self.plan_path, self.import_path, self.dirty = plan, None, Path(path), True
            self.reset_history(saved=False)
            self.refresh()
            overload = f"; {len(self.resource_warnings)} overallocated resource(s)" if self.resource_warnings else ""
            self.statusBar().showMessage(f"Opened {Path(path).name} as an imported copy{overload}; save a .pxplan to keep changes")
            if notes:
                QMessageBox.warning(self, "Import details", "\n".join(notes[:30]) + ("\n..." if len(notes) > 30 else ""))
        except Exception as exc:
            QMessageBox.critical(self, "Cannot import MPP", str(exc))

    def export_project(self):
        self.show_export_dialog()

    def show_export_dialog(self, initial_format="mpp", output_path=None):
        try:
            initial_path = output_path or (self.plan_path.with_suffix(f".{initial_format}") if self.plan_path else None)
            dialog = ExportDialog(self, initial_format, initial_path, self.template,
                                  (self.plan_path, self.import_path, self.template))
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.output_path is None:
                return
            path = dialog.output_path
            selected = dialog.selected_format()
            if selected == "mpp":
                self.template = dialog.template_path
                notes = export_mpp(self.plan, self.template, path, (self.import_path,) if self.import_path else ())
                QMessageBox.information(self, "MPP exported", "Saved a new MPP copy. Verify it in Microsoft Project. "
                                        "Save a .pxplan to keep your editable working file." +
                                        ("\n\nWriter warnings:\n" + "\n".join(notes) if notes else ""))
            elif selected == "html":
                export_html(self.plan, path, dialog.show_time.isChecked())
                self.statusBar().showMessage(f"Exported read-only web page: {path.name}")
            elif selected == "pdf":
                export_pdf(self.plan, path, dialog.paper.currentData(), dialog.show_time.isChecked())
                self.statusBar().showMessage(f"Exported read-only PDF: {path.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Cannot export project", str(exc))

    @staticmethod
    def edited_periods(box, current: list[str]) -> list[str]:
        """Entries typed in a settings box. A box left as it was keeps its entries exactly, so a plan
        from an older version that repeats a date can still have its other settings saved."""
        text = box.toPlainText()
        if text == "\n".join(current):
            return list(current)
        return [format_period(period) for period in parse_periods(text)]

    def settings(self):
        dialog = SettingsDialog(self.plan, self, self.digit_edits_duration, self.date_format,
                                self.show_icon_labels, self.preferences.fileName(), self.confirm_task_deletion, self.density,
                                self.visible_task_columns, self.color_collapsed_summaries)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def update():
                self.plan.title = dialog.title.text().strip() or "Untitled project"
                self.plan.start = dialog.start.text().strip()
                self.plan.holidays = self.edited_periods(dialog.holidays, self.plan.holidays)
                self.plan.working_days = self.edited_periods(dialog.working_days, self.plan.working_days)
            if self.mutate(update, self.selected_uid()):
                self.digit_edits_duration = dialog.digit_edits_duration.isChecked()
                self.confirm_task_deletion = dialog.confirm_task_deletion.isChecked()
                self.show_icon_labels = dialog.show_icon_labels.isChecked()
                self.color_collapsed_summaries = dialog.color_collapsed_summaries.isChecked()
                self.visible_task_columns = {key: checkbox.isChecked()
                                             for key, checkbox in dialog.column_visibility.items()}
                previous_density = self.density
                self.density = dialog.density_selector.currentData()
                previous_date_format = self.date_format
                self.date_format = dialog.date_format.currentData()
                self.preferences.setValue("keyboard/digit_edits_duration", self.digit_edits_duration)
                self.preferences.setValue("editing/confirm_task_deletion", self.confirm_task_deletion)
                self.preferences.setValue(DENSITY_PREFERENCE_KEY, self.density)
                self.preferences.setValue("display/show_icon_labels", self.show_icon_labels)
                self.preferences.setValue("display/date_format", self.date_format)
                self.preferences.setValue("display/color_collapsed_summaries", self.color_collapsed_summaries)
                for key, visible in self.visible_task_columns.items():
                    self.preferences.setValue(f"display/task_column_visibility/{key}", visible)
                self.sync_preferences()
                self.apply_toolbar_labels()
                self.apply_column_visibility()
                self.gantt.update()
                if self.density != previous_density:
                    self.apply_density()
                if self.date_format != previous_date_format:
                    self.refresh()

    def manage_resources(self):
        dialog = ResourceSheetDialog(self.plan, self, density=self.density)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def update():
                migrate_legacy_resources(self.plan)
                self.plan.resources = copy.deepcopy(dialog.result_resources)
            self.mutate(update, self.selected_uid())

    def assign_resources(self):
        uid = self.selected_uid()
        if uid is None:
            QMessageBox.information(self, "Assign resources", "Select a task first.")
            return
        draft = copy.deepcopy(self.plan)
        migrate_legacy_resources(draft)
        task = next(task for task in draft.tasks if task.uid == uid)
        dialog = AssignResourcesDialog(draft, task, self, density=self.density)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            assignments = dialog.selected_assignments()
            def update():
                migrate_legacy_resources(self.plan)
                next(task for task in self.plan.tasks if task.uid == uid).assignments = assignments
            self.mutate(update, uid)

    def add_task(self):
        uid = max((t.uid for t in self.plan.tasks), default=0) + 1
        task = Task(uid=uid)
        dialog = TaskDialog(task, self.plan, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def add():
                dialog.apply(task, self.plan)
                self.plan.tasks.append(task)
            self.mutate(add, uid)

    def add_inline_task(self, name, before_uid=None):
        name = name.strip()
        if not name:
            return
        uid = max((t.uid for t in self.plan.tasks), default=0) + 1
        def insert():
            if before_uid is None:
                self.plan.tasks.append(Task(uid=uid, name=name))
                return
            index = next((i for i, task in enumerate(self.plan.tasks) if task.uid == before_uid), None)
            if index is None:
                raise ValueError("The task to insert above no longer exists")
            self.plan.tasks.insert(index, Task(uid=uid, name=name, parent=self.plan.tasks[index].parent))
        self.mutate(insert, uid)

    def edit_task(self):
        uid = self.selected_uid()
        if uid is None:
            return
        dialog = TaskDialog(next(t for t in self.plan.tasks if t.uid == uid), self.plan, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.mutate(lambda: dialog.apply(next(t for t in self.plan.tasks if t.uid == uid), self.plan), uid)

    def delete_task(self):
        uid = self.selected_uid()
        if uid is None:
            return
        ids = descendants(self.plan, uid)
        if (self.confirm_task_deletion and
                QMessageBox.question(self, "Delete task", f"Delete {len(ids)} task(s) and their dependency links?") != QMessageBox.StandardButton.Yes):
            return
        def delete():
            self.plan.tasks = [t for t in self.plan.tasks if t.uid not in ids]
            for task in self.plan.tasks:
                task.predecessors = [pred for pred in task.predecessors if parse_dependency(pred).uid not in ids]
        self.mutate(delete)

    def indent_task(self):
        uid = self.selected_uid()
        if uid is not None:
            self.mutate(lambda: indent(self.plan, uid), uid)

    def outdent_task(self):
        uid = self.selected_uid()
        if uid is not None:
            self.mutate(lambda: outdent(self.plan, uid), uid)


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(application_icon())
    app.aboutToQuit.connect(release_rows)
    app.setApplicationName("OnePyProject")
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
