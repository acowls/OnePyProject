import copy
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from onepyproject.app import MainWindow
from onepyproject.model import Plan, Task


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "A"), Task(2, "B"), Task(3, "C")])
    window.reset_history(saved=True)
    window.resize(950, 500)
    window.show()
    window.refresh()
    app.processEvents()
    yield window
    window.dirty = False
    window.close()
    app.clipboard().clear()


def select(window, *uids):
    window.tree.clearSelection()
    for uid in uids:
        window.items[uid].setSelected(True)
    if uids:
        window.tree.setCurrentItem(window.items[uids[-1]], 0, QItemSelectionModel.SelectionFlag.NoUpdate)


def menu_actions(window):
    menu = window.task_context_menu()
    return menu, {action.text(): action for action in menu.actions()}


def test_context_menu_enables_only_valid_actions(window):
    select(window, 1)
    menu, actions = menu_actions(window)
    assert list(actions) == ["Cut", "Copy", "Paste", "Edit", "Delete", "Unlink", "Link", "New task"]
    assert all(actions[name].isEnabled() for name in ("Cut", "Copy", "Edit", "Delete", "New task"))
    assert all(not actions[name].isEnabled() for name in ("Paste", "Unlink", "Link"))
    assert all(not action.icon().isNull() for action in actions.values())
    select(window, 1, 2)
    menu, actions = menu_actions(window)
    assert actions["Link"].isEnabled() and not actions["Unlink"].isEnabled()
    assert not actions["Edit"].isEnabled() and not actions["Delete"].isEnabled()
    actions["Link"].trigger()
    assert window.plan.tasks[1].predecessors == [1]
    menu, actions = menu_actions(window)
    assert not actions["Link"].isEnabled() and actions["Unlink"].isEnabled()
    actions["Unlink"].trigger()
    assert window.plan.tasks[1].predecessors == []
    window.undo()
    assert window.plan.tasks[1].predecessors == [1]


def test_invalid_links_and_summary_tasks_are_disabled(window):
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "A", predecessors=[2]), Task(2, "B"),
                                                   Task(3, "Summary"), Task(4, "Child", parent=3)])
    window.refresh()
    select(window, 1, 2)
    menu, actions = menu_actions(window)
    assert not actions["Link"].isEnabled() and actions["Unlink"].isEnabled()
    select(window, 2, 3)
    menu, actions = menu_actions(window)
    assert not actions["Link"].isEnabled() and not actions["Unlink"].isEnabled()


def test_paste_tracks_clipboard_and_cut_destination(window):
    select(window, 1)
    window.copy_rows()
    menu, actions = menu_actions(window)
    assert actions["Paste"].isEnabled()
    window.cut_rows()
    menu, actions = menu_actions(window)
    assert not actions["Paste"].isEnabled()
    select(window, 2)
    menu, actions = menu_actions(window)
    assert actions["Paste"].isEnabled()
    window.edit_cell(3, 1, "Changed")
    menu, actions = menu_actions(window)
    assert not actions["Paste"].isEnabled()


def test_right_click_preserves_multi_selection_or_selects_clicked_row(window, app, monkeypatch):
    captured = []
    make_menu = window.task_context_menu
    def capture(parent):
        menu = make_menu(parent)
        class TestMenu:
            def exec(self, *args):
                captured.append((window.selected_uids(), window.selected_uid(),
                                 {action.text(): action.isEnabled() for action in menu.actions()}))
            def deleteLater(self):
                menu.deleteLater()
        return TestMenu()
    monkeypatch.setattr(window, "task_context_menu", capture)
    select(window, 1, 2)
    viewport = window.tree.viewport()
    def right_click(uid):
        position = window.tree.visualItemRect(window.items[uid]).center()
        QTest.mouseClick(viewport, Qt.MouseButton.RightButton, pos=position)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, viewport.mapToGlobal(position))
        app.sendEvent(viewport, event)
    right_click(1)
    assert captured[-1][0] == [1, 2] and captured[-1][1] == 1 and captured[-1][2]["Link"]
    right_click(3)
    assert captured[-1][0] == [3] and captured[-1][1] == 3 and captured[-1][2]["Edit"]


def test_empty_area_menu_and_readonly_preview(window, app, monkeypatch):
    select(window, 1)
    window.copy_rows()
    position = window.tree.visualItemRect(window.tree.placeholder).center()
    window.tree.select_context_item(position)
    menu, actions = menu_actions(window)
    assert window.selected_uids() == []
    assert actions["Paste"].isEnabled() and actions["New task"].isEnabled()
    assert not actions["Cut"].isEnabled() and not actions["Edit"].isEnabled()
    after = copy.deepcopy(window.plan)
    after.tasks[0].duration = 2
    window.show_ai_preview(window.plan, after)
    menu, actions = menu_actions(window)
    assert all(not action.isEnabled() for action in actions.values())
    called = []
    monkeypatch.setattr(window, "task_context_menu", lambda *args: called.append(True))
    position = window.tree.visualItemRect(window.items[1]).center()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, window.tree.viewport().mapToGlobal(position))
    app.sendEvent(window.tree.viewport(), event)
    assert not called
    window.clear_ai_preview()


def test_gantt_context_menu_targets_row_and_ignores_date_header(window, app, monkeypatch):
    captured = []
    def capture(parent):
        class TestMenu:
            def exec(self, *args):
                captured.append((window.selected_uids(), window.selected_uid()))
            def deleteLater(self):
                pass
        return TestMenu()
    monkeypatch.setattr(window, "task_context_menu", capture)
    select(window, 1, 2)
    y = window.tree.header().height() + window.tree.visualItemRect(window.items[1]).center().y()
    position = window.gantt.rect().topLeft() + window.gantt.rect().center()
    position.setY(y)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, window.gantt.mapToGlobal(position))
    app.sendEvent(window.gantt, event)
    assert captured == [([1, 2], 1)]
    position.setY(1)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, window.gantt.mapToGlobal(position))
    app.sendEvent(window.gantt, event)
    assert len(captured) == 1


def test_context_menu_does_not_apply_stale_actions(window, monkeypatch):
    select(window, 1)
    calls = []
    monkeypatch.setattr(window, "add_task", lambda: calls.append(True))
    menu, actions = menu_actions(window)
    window.edit_cell(1, 1, "Changed while menu open")
    actions["New task"].trigger()
    assert not calls and "open the menu again" in window.statusBar().currentMessage()


@pytest.mark.parametrize("preview", [False, True])
def test_gantt_header_is_not_overpainted_when_rows_scroll(window, app, preview):
    window.plan = Plan(start="2026-11-16", tasks=[Task(uid, f"Task {uid}") for uid in range(1, 81)])
    window.reset_history(saved=True)
    window.refresh()
    if preview:
        after = copy.deepcopy(window.plan)
        after.tasks[0].duration = 3
        window.show_ai_preview(window.plan, after)
    app.processEvents()
    header = window.tree.header().height()
    before = window.gantt.grab().toImage()
    before_header = before.copy(0, 0, before.width(), round(header * before.devicePixelRatio()))
    assert window.tree.verticalScrollBar().maximum() > 0
    window.tree.verticalScrollBar().setValue(4)
    app.processEvents()
    after = window.gantt.grab().toImage()
    assert after.copy(0, 0, after.width(), round(header * after.devicePixelRatio())) == before_header
    if preview:
        window.clear_ai_preview()
