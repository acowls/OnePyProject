import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLineEdit

from onepyproject.app import MainWindow
from onepyproject.model import Plan, Resource, ResourceAssignment, Task


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _editor(app, tmp_path, plan):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = plan
    window.reset_history(saved=True)
    window.show()
    window.refresh()
    app.processEvents()
    window.tree.setCurrentItem(window.items[1], 6)
    window.tree.editItem(window.items[1], 6)
    app.processEvents()
    editor = window.tree.findChild(QLineEdit)
    assert editor is not None and editor.completer() is not None
    return window, editor


def _choose(editor, name):
    completer = editor.completer()
    model = completer.completionModel()
    matches = [model.index(row, 0) for row in range(model.rowCount()) if model.index(row, 0).data() == name]
    assert matches, name
    completer.popup().setCurrentIndex(matches[0])
    QTest.keyClick(editor, Qt.Key.Key_Return)


def _finish(app, window, editor):
    assert window.tree.state() == QAbstractItemView.State.EditingState
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()


def test_inline_completion_is_case_insensitive_and_undoable(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada"), Resource(2, "Alice"), Resource(3, "Ben")], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    QTest.keyClicks(editor, "ad")
    app.processEvents()
    assert editor.completer().popup().isVisible()
    assert editor.completer().completionCount() == 1
    _choose(editor, "Ada")
    app.processEvents()
    assert editor.text() == "Ada"
    assert not window.plan.tasks[0].assignments  # accepting a suggestion doesn't save the cell yet
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(1, 100)]
    assert [resource.name for resource in window.plan.resources] == ["Ada", "Alice", "Ben"]
    window.undo()
    assert not window.plan.tasks[0].assignments
    window.dirty = False
    window.close()


def test_arrow_keys_and_tab_choose_suggestion_without_closing_editor(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada"), Resource(2, "Adam")], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    QTest.keyClicks(editor, "Ad")
    app.processEvents()
    assert editor.completer().popup().currentIndex().data() == "Ada"
    QTest.keyClick(editor, Qt.Key.Key_Down)
    assert editor.completer().popup().currentIndex().data() == "Adam"
    assert editor.text() == "Ad"  # navigating must not preview into the whole cell
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    app.processEvents()
    assert editor.text() == "Adam"
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(2, 100)]
    window.dirty = False
    window.close()


def test_completion_replaces_only_current_name_and_preserves_allocations(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada"), Resource(2, "Bert"), Resource(3, "Ben"), Resource(4, "Cara")],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1, 50), ResourceAssignment(2, 25), ResourceAssignment(4, 75)])])
    window, editor = _editor(app, tmp_path, plan)
    start = editor.text().index("Bert")
    editor.setSelection(start, 4)
    QTest.keyClicks(editor, "Be")
    app.processEvents()
    assert editor.completer().completionCount() == 2
    _choose(editor, "Ben")
    app.processEvents()
    assert editor.text() == "Ada[50%], Ben[25%], Cara[75%]"
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(1, 50), ResourceAssignment(3, 25), ResourceAssignment(4, 75)]
    window.undo()
    assert window.plan.tasks[0].assignments[1] == ResourceAssignment(2, 25)
    window.dirty = False
    window.close()


def test_completion_quotes_resource_names_with_commas_and_keeps_suffix(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada, Senior"), Resource(2, "Ben")], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    editor.setText("Adx[50%], Ben")
    editor.setCursorPosition(3)
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    app.processEvents()
    assert editor.completer().completionCount() == 1
    _choose(editor, "Ada, Senior")
    app.processEvents()
    assert editor.text() == '"Ada, Senior[50%]", Ben'
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(1, 50), ResourceAssignment(2, 100)]
    window.dirty = False
    window.close()


def test_completion_inside_existing_quoted_name_preserves_other_resources(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada, Senior"), Resource(2, "Ada, Junior"), Resource(3, "Ben")],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1, 50), ResourceAssignment(3, 100)])])
    window, editor = _editor(app, tmp_path, plan)
    start = editor.text().index("Senior")
    editor.setSelection(start, len("Senior"))
    QTest.keyClicks(editor, "Ju")
    app.processEvents()
    _choose(editor, "Ada, Junior")
    app.processEvents()
    assert editor.text() == '"Ada, Junior[50%]", Ben'
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(2, 50), ResourceAssignment(3, 100)]
    window.dirty = False
    window.close()


def test_mouse_completion_escapes_quoted_names(app, tmp_path):
    name = 'Alice "Lead", East'
    plan = Plan(resources=[Resource(1, name)], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    QTest.keyClicks(editor, "Ali")
    app.processEvents()
    completer = editor.completer()
    index = completer.completionModel().index(0, 0)
    rect = completer.popup().visualRect(index)
    QTest.mouseClick(completer.popup().viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, rect.center())
    app.processEvents()
    assert editor.text() == '"Alice ""Lead"", East"'
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(1, 100)]
    window.dirty = False
    window.close()


def test_editing_allocation_does_not_trigger_name_completion(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada")],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1, 50)])])
    window, editor = _editor(app, tmp_path, plan)
    start = editor.text().index("50")
    editor.setSelection(start, 2)
    QTest.keyClicks(editor, "75")
    app.processEvents()
    assert not editor.completer().popup().isVisible()
    _finish(app, window, editor)
    assert window.plan.tasks[0].assignments == [ResourceAssignment(1, 75)]
    window.dirty = False
    window.close()


def test_new_resource_without_matching_suggestion_still_works(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    QTest.keyClicks(editor, "Zoe")
    app.processEvents()
    assert not editor.completer().popup().isVisible()
    _finish(app, window, editor)
    assert [resource.name for resource in window.plan.resources] == ["Ada", "Zoe"]
    assert window.plan.tasks[0].assignments == [ResourceAssignment(2, 100)]
    window.dirty = False
    window.close()


def test_cancel_after_completion_does_not_change_project(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    QTest.keyClicks(editor, "Ad")
    app.processEvents()
    _choose(editor, "Ada")
    app.processEvents()
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    app.processEvents()
    assert not window.plan.tasks[0].assignments
    assert not window.dirty and not window._undo
    window.close()


def test_escape_dismisses_suggestions_without_replacing_typed_name(app, tmp_path):
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[Task(1, "Task")])
    window, editor = _editor(app, tmp_path, plan)
    QTest.keyClicks(editor, "Ad")
    app.processEvents()
    assert editor.completer().popup().isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    assert not editor.completer().popup().isVisible()
    assert editor.text() == "Ad"
    _finish(app, window, editor)
    assert [resource.name for resource in window.plan.resources] == ["Ada", "Ad"]
    window.dirty = False
    window.close()
