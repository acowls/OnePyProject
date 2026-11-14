"""Dialogs for the shared resource sheet and task assignments."""
from __future__ import annotations

import copy
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QHeaderView, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .model import Plan, Resource, ResourceAssignment, Task, schedule
from .resources import migrate_legacy_resources, overallocation_warnings
from .dependencies import task_numbers
from .ui_preferences import DEFAULT_DENSITY, DENSITY_HEIGHTS


def _readonly(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class ResourceSheetDialog(QDialog):
    def __init__(self, plan: Plan, parent=None, density=DEFAULT_DENSITY):
        super().__init__(parent)
        self.setWindowTitle("Resource sheet")
        self.resize(650, 420)
        self.plan = copy.deepcopy(plan)
        migrate_legacy_resources(self.plan)
        self.result_resources: list[Resource] = []
        self._next_uid = max((resource.uid for resource in self.plan.resources), default=0) + 1
        layout = QVBoxLayout(self)
        intro = QLabel("Manage people or teams and their maximum available allocation. Overallocations are warnings; tasks are not moved automatically.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ID", "Resource name", "Max units %", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setMinimumSectionSize(0)
        self.table.verticalHeader().setDefaultSectionSize(DENSITY_HEIGHTS.get(density, DENSITY_HEIGHTS[DEFAULT_DENSITY]))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(2, 110)
        self.table.setStyleSheet("QTableWidget { color: #24354a; background: white; } QTableWidget::item:selected { color: #183753; background: #d5e9fa; }")
        layout.addWidget(self.table)
        for resource in self.plan.resources:
            self._append(resource)
        controls = QHBoxLayout()
        add = QPushButton("Add resource")
        remove = QPushButton("Remove resource")
        add.clicked.connect(self.add_resource)
        remove.clicked.connect(self.remove_resource)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch()
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.itemChanged.connect(self.update_status)
        self.update_status()

    def _append(self, resource: Resource):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, _readonly(str(resource.uid)))
        self.table.setItem(row, 1, QTableWidgetItem(resource.name))
        self.table.setItem(row, 2, QTableWidgetItem(str(float(resource.max_units)).removesuffix(".0")))
        self.table.setItem(row, 3, _readonly(""))

    def add_resource(self):
        existing = {self.table.item(row, 1).text().strip().casefold() for row in range(self.table.rowCount())}
        name = "New resource"
        suffix = 2
        while name.casefold() in existing:
            name = f"New resource {suffix}"
            suffix += 1
        self.table.blockSignals(True)
        self._append(Resource(self._next_uid, name))
        self._next_uid += 1
        self.table.blockSignals(False)
        self.update_status()
        self.table.setCurrentCell(self.table.rowCount() - 1, 1)
        self.table.editItem(self.table.item(self.table.rowCount() - 1, 1))

    def remove_resource(self):
        row = self.table.currentRow()
        if row < 0:
            return
        uid = int(self.table.item(row, 0).text())
        numbers = task_numbers(self.plan.tasks)
        affected = [numbers[task.uid] for task in self.plan.tasks if any(a.resource_uid == uid for a in task.assignments)]
        if affected:
            QMessageBox.warning(self, "Resource is assigned",
                                "Remove this resource from its tasks before deleting it. "
                                f"Assigned task IDs: {', '.join(map(str, affected))}")
            return
        self.table.removeRow(row)
        self.update_status()

    def read_resources(self) -> list[Resource]:
        result = []
        for row in range(self.table.rowCount()):
            try:
                uid = int(self.table.item(row, 0).text())
                name = self.table.item(row, 1).text().strip()
                maximum = float(self.table.item(row, 2).text().strip().removesuffix("%"))
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"Resource row {row + 1}: enter a name and a numeric maximum percentage") from exc
            result.append(Resource(uid, name, maximum))
        draft = copy.deepcopy(self.plan)
        draft.resources = result
        schedule(draft)
        return result

    def update_status(self, *_):
        try:
            resources = self.read_resources()
            draft = copy.deepcopy(self.plan)
            draft.resources = resources
            warnings = overallocation_warnings(draft)
        except (ValueError, OverflowError, KeyError):
            warnings = None
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                uid = int(self.table.item(row, 0).text())
                intervals = warnings.get(uid, []) if warnings is not None else []
                item = self.table.item(row, 3)
                item.setText("Check details" if warnings is None else ("Overallocated" if intervals else "OK"))
                item.setForeground(QColor("#895800" if warnings is None else ("#a72a25" if intervals else "#315f46")))
                item.setToolTip("\n".join(
                    f"{v.start:%Y-%m-%d %H:%M} – {v.finish:%Y-%m-%d %H:%M}: {v.units:g}% assigned / {v.capacity:g}% available"
                    for v in intervals[:20]))
        finally:
            self.table.blockSignals(False)

    def accept(self):
        try:
            self.result_resources = self.read_resources()
        except (ValueError, OverflowError, KeyError) as exc:
            QMessageBox.warning(self, "Invalid resources", str(exc))
            return
        super().accept()


class AssignResourcesDialog(QDialog):
    def __init__(self, plan: Plan, task: Task, parent=None, density=DEFAULT_DENSITY):
        super().__init__(parent)
        self.setWindowTitle(f"Assign resources — {task.name}")
        self.resize(600, 380)
        self.task = task
        self.rows = []
        assigned = {assignment.resource_uid: assignment.units for assignment in task.assignments}
        self.is_summary = any(other.parent == task.uid for other in plan.tasks)
        layout = QVBoxLayout(self)
        note = QLabel("Summary assignments are not scheduled; you can remove existing ones here." if self.is_summary else
                      "Select resources and set each allocation. Work is shared using the project calendar; no automatic levelling is performed.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Assign", "Resource", "Allocation %", "Max %"])
        self.table.verticalHeader().setMinimumSectionSize(0)
        self.table.verticalHeader().setDefaultSectionSize(DENSITY_HEIGHTS.get(density, DENSITY_HEIGHTS[DEFAULT_DENSITY]))
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 65)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 80)
        self.table.setStyleSheet("QTableWidget { color: #24354a; background: white; } QTableWidget::item:selected { color: #183753; background: #d5e9fa; }")
        layout.addWidget(self.table)
        for resource in plan.resources:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = _readonly("")
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if self.is_summary and resource.uid not in assigned:
                check.setFlags(check.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked if resource.uid in assigned else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, _readonly(resource.name))
            units = QDoubleSpinBox()
            original = assigned.get(resource.uid, 100)
            decimals = min(10, max(2, -Decimal(str(original)).as_tuple().exponent))
            units.setDecimals(decimals)
            units.setRange(10 ** -decimals, max(10000, original))
            units.setValue(original)
            units.setMinimumHeight(0)
            units.setMaximumHeight(DENSITY_HEIGHTS.get(density, DENSITY_HEIGHTS[DEFAULT_DENSITY]))
            units.setToolTip(f"Exact allocation: {original:.17g}%")
            units.setProperty("original_allocation", original)
            units.setProperty("allocation_changed", False)
            units.valueChanged.connect(lambda _value, editor=units: editor.setProperty("allocation_changed", True))
            units.setSuffix(" %")
            self.table.setCellWidget(row, 2, units)
            self.table.setItem(row, 3, _readonly(f"{resource.max_units:g}"))
            self.rows.append((resource.uid, check, units))
        if not plan.resources:
            layout.addWidget(QLabel("No resources yet. Add them with the Resources button first."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_assignments(self) -> list[ResourceAssignment]:
        return [ResourceAssignment(uid, units.value() if units.property("allocation_changed") else units.property("original_allocation"))
                for uid, check, units in self.rows
                if check.checkState() == Qt.CheckState.Checked]

    def accept(self):
        if not self.is_summary and self.task.mode == "work" and not self.selected_assignments():
            QMessageBox.warning(self, "Resource required", "Fixed-work tasks need at least one assigned resource.")
            return
        super().accept()
