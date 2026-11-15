import copy
import os
import subprocess
import sys

import pytest
from PySide6.QtCore import QItemSelectionModel, QMimeData, QSettings, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from onepyproject.app import MainWindow
from onepyproject.model import Plan, Resource, ResourceAssignment, Task


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    previous = QMimeData()
    existing = app.clipboard().mimeData()
    if existing is not None:
        for kind in existing.formats():
            previous.setData(kind, existing.data(kind))
    window = MainWindow(QSettings(str(tmp_path / "clipboard.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(10, "Phase"), Task(20, "Design", parent=10),
                       Task(30, "Review", parent=10, predecessors=["20SS+1.234567890123456789e-3d"]),
                       Task(40, "Release", predecessors=[30])])
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    yield window
    window.dirty = False
    window.close()
    if app.platformName() == "offscreen":
        app.clipboard().clear()
    else:
        app.clipboard().setMimeData(previous)


def select(window, *uids):
    window.tree.clearSelection()
    for uid in uids:
        window.items[uid].setSelected(True)
    if uids:
        window.tree.selectionModel().setCurrentIndex(window.tree.indexFromItem(window.items[uids[-1]]),
                                                     QItemSelectionModel.SelectionFlag.NoUpdate)


def test_copy_parent_includes_children_and_remaps_internal_links(window):
    original = copy.deepcopy(window.plan)
    select(window, 10, 20)
    window.copy_rows()
    select(window, 40)
    window.paste_rows()
    assert [task.name for task in window.plan.tasks] == ["Phase", "Design", "Review", "Release", "Phase", "Design", "Review"]
    parent, design, review = window.plan.tasks[-3:]
    assert design.parent == review.parent == parent.uid
    assert review.predecessors == [f"{design.uid}SS+1.234567890123456789e-3d"]
    assert window.plan.tasks[3].predecessors == [30] and window.dirty
    window.undo()
    assert window.plan == original and not window.dirty
    window.redo()
    assert window.plan.tasks[-1].predecessors == [f"{design.uid}SS+1.234567890123456789e-3d"]


def test_cut_paste_moves_subtree_and_keeps_successor_links(window):
    original = copy.deepcopy(window.plan)
    select(window, 10)
    window.cut_rows()
    assert window.plan == original and not window.dirty
    select(window, 40)
    window.paste_rows()
    assert [task.uid for task in window.plan.tasks] == [40, 10, 20, 30]
    assert window.plan.tasks[0].predecessors == [30]
    assert window.plan.tasks[-1].predecessors == ["20SS+1.234567890123456789e-3d"]
    assert len(window._undo) == 1
    window.undo()
    assert window.plan == original and not window.dirty


def test_copy_multiple_rows_flattens_roots_and_retains_link_between_them(window):
    select(window, 20, 30)
    window.copy_rows()
    select(window, 40)
    window.paste_rows()
    design, review = window.plan.tasks[-2:]
    assert design.parent == review.parent == 0
    assert review.predecessors == [f"{design.uid}SS+1.234567890123456789e-3d"]


def test_cut_rejects_pasting_into_own_subtree_without_losing_rows(window, monkeypatch):
    original = copy.deepcopy(window.plan)
    select(window, 10)
    window.cut_rows()
    select(window, 20)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.paste_rows()
    assert errors and window.plan == original and not window.dirty
    select(window, 40)
    window.paste_rows()
    assert [task.uid for task in window.plan.tasks] == [40, 10, 20, 30]


def test_cut_does_not_delete_if_clipboard_or_project_changes(window, monkeypatch, app):
    select(window, 20)
    window.cut_rows()
    app.clipboard().setText("unrelated text")
    select(window, 40)
    window.paste_rows()
    assert len(window.plan.tasks) == 4 and not window.dirty
    select(window, 20)
    window.cut_rows()
    window.edit_cell(40, 1, "Changed")
    before = copy.deepcopy(window.plan)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    select(window, 40)
    window.paste_rows()
    assert window.plan == before and errors and "unchanged source" in errors[-1]


def test_copy_between_projects_drops_external_links_and_copies_resources(window, app, tmp_path):
    window.plan = Plan(start="2026-11-16", resources=[Resource(2, "Ada", 80)], tasks=[Task(1, "External"),
                       Task(2, "Copy", predecessors=[1], mode="work", work=16,
                            assignments=[ResourceAssignment(2, 50)])])
    window.reset_history(saved=True)
    window.refresh()
    select(window, 2)
    window.copy_rows()
    target = MainWindow(QSettings(str(tmp_path / "other.ini"), QSettings.Format.IniFormat))
    target.plan = Plan(start="2026-11-16", tasks=[Task(1, "Unrelated")])
    target.reset_history(saved=True)
    target.refresh()
    select(target, 1)
    target.paste_rows()
    pasted = target.plan.tasks[-1]
    assert pasted.predecessors == [] and pasted.assignments[0].resource_uid == target.plan.resources[0].uid
    assert target.plan.resources[0].name == "Ada" and target.plan.resources[0].max_units == 80
    assert "1 unsupported predecessor" in target.statusBar().currentMessage()
    target.undo()
    assert len(target.plan.tasks) == 1 and not target.plan.resources and not target.dirty
    target.close()


def test_copy_preserves_meaningful_external_predecessors_for_repeated_paste(window):
    select(window, 30)
    window.copy_rows()
    select(window, 40)
    window.paste_rows()
    assert window.plan.tasks[-1].predecessors == ["20SS+1.234567890123456789e-3d"]
    window.paste_rows()
    assert window.plan.tasks[-1].predecessors == ["20SS+1.234567890123456789e-3d"]


def test_standard_shortcuts_work_for_rows_but_not_intercept_text_edit(window, app):
    select(window, 20)
    window.tree.setFocus()
    for action, key in ((window.copy_action, Qt.Key.Key_C), (window.paste_action, Qt.Key.Key_V)):
        assert action.shortcut() == QKeySequence(QKeySequence.StandardKey.Copy if key == Qt.Key.Key_C else QKeySequence.StandardKey.Paste)
        QTest.keyClick(window.tree, key, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
    assert len(window.plan.tasks) == 5
    window.tree.setCurrentItem(window.items[20], 1)
    window.tree.editItem(window.items[20], 1)
    app.processEvents()
    editor = next(item for item in window.tree.findChildren(QLineEdit) if item.isVisible())
    editor.selectAll()
    QTest.keyClick(editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert app.clipboard().text() == "Design"
    QTest.keyClick(editor, Qt.Key.Key_Escape)


def test_cut_and_paste_shortcuts_move_as_one_action(window, app):
    select(window, 10)
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    assert len(window.plan.tasks) == 4 and not window.dirty
    select(window, 40)
    QTest.keyClick(window.tree, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert [task.uid for task in window.plan.tasks] == [40, 10, 20, 30]
    window.undo()
    assert [task.uid for task in window.plan.tasks] == [10, 20, 30, 40]


def test_copy_drops_predecessor_deleted_before_paste(window, monkeypatch):
    select(window, 30)
    window.copy_rows()
    select(window, 20)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.delete_task()
    select(window, 40)
    window.paste_rows()
    assert not window.plan.tasks[-1].predecessors
    assert "1 unsupported predecessor" in window.statusBar().currentMessage()


def test_clipboard_is_released_when_the_application_quits(tmp_path):
    script = """
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from onepyproject.app import MainWindow
from onepyproject.clipboard import release_rows
from onepyproject.model import Plan, Task
import sys
app = QApplication([])
app.aboutToQuit.connect(release_rows)
window = MainWindow(QSettings(sys.argv[1], QSettings.Format.IniFormat))
window.plan = Plan(tasks=[Task(1, 'Copied task')])
window.refresh()
window.tree.setCurrentItem(window.items[1], 1)
window.copy_rows()
QTimer.singleShot(0, app.quit)
app.exec()
assert 'Copied task' in app.clipboard().text()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "settings.ini")],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
