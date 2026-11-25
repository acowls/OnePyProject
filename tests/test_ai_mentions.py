import copy
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from onepyproject.ai.provider import AIResult, normalize_config
from onepyproject.ai.ui import ConsentDialog
from onepyproject.app import MainWindow
from onepyproject.model import Plan, Resource, Task


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def assistant(app, tmp_path):
    window = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.plan = Plan(start="2026-11-16", resources=[Resource(1, "Craig"), Resource(2, "Craig Smith"),
                       Resource(3, 'Cara "Lead", East'), Resource(4, "Zoë")], tasks=[Task(1, "Task")])
    window.reset_history(saved=True)
    window.refresh()
    window.show()
    window.open_ai()
    app.processEvents()
    dock = window._ai_dialog
    dock.prompt.setFocus()
    app.processEvents()
    yield dock
    window.dirty = False
    window.close()


def refresh(app):
    app.processEvents()
    app.processEvents()


def choices(editor):
    model = editor.completer.completionModel()
    return [model.index(row, 0).data() for row in range(model.rowCount())]


def test_at_sign_lists_resources_before_filtering(assistant, app):
    QTest.keyClicks(assistant.prompt, "@")
    refresh(app)
    assert assistant.prompt.completer.popup().isVisible()
    assert choices(assistant.prompt) == ['Cara "Lead", East', "Craig", "Craig Smith", "Zoë"]


def test_popup_updates_while_typing_one_character_at_a_time(assistant, app):
    for character in "@cr":
        QTest.keyClicks(assistant.prompt, character)
        refresh(app)
    assert assistant.prompt.hasFocus() and assistant.prompt.completer.popup().isVisible()
    assert choices(assistant.prompt) == ["Craig", "Craig Smith"]


def test_mention_filters_case_insensitively_and_accepts_with_enter(assistant, app):
    editor = assistant.prompt
    before = copy.deepcopy(assistant.window.plan)
    QTest.keyClicks(editor, "Assign @cr")
    refresh(app)
    assert editor.completer.popup().isVisible()
    assert choices(editor) == ["Craig", "Craig Smith"]
    QTest.keyClick(editor, Qt.Key.Key_Return)
    refresh(app)
    assert editor.toPlainText() == "Assign @Craig"
    assert not editor.completer.popup().isVisible()
    assert assistant.window.plan == before and not assistant.window.dirty


def test_arrow_and_tab_select_quoted_multiword_resource(assistant, app):
    editor = assistant.prompt
    QTest.keyClicks(editor, "@cra")
    refresh(app)
    QTest.keyClick(editor, Qt.Key.Key_Down)
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    refresh(app)
    assert editor.toPlainText() == '@"Craig Smith"'
    assert not editor.completer.popup().isVisible()


def test_mouse_selection_escapes_punctuation_and_quotes(assistant, app):
    editor = assistant.prompt
    QTest.keyClicks(editor, "@car")
    refresh(app)
    popup = editor.completer.popup()
    assert popup.isVisible()
    index = editor.completer.completionModel().index(0, 0)
    QTest.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=popup.visualRect(index).center())
    refresh(app)
    assert editor.toPlainText() == '@"Cara \\"Lead\\", East"'


def test_escape_no_matches_and_email_do_not_replace_text(assistant, app):
    editor = assistant.prompt
    QTest.keyClicks(editor, "@cr")
    refresh(app)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    refresh(app)
    assert editor.toPlainText() == "@cr" and not editor.completer.popup().isVisible()
    editor.setPlainText("unknown @zzz")
    editor.moveCursor(QTextCursor.MoveOperation.End)
    refresh(app)
    assert not editor.completer.popup().isVisible()
    editor.setPlainText("foo@cra")
    editor.moveCursor(QTextCursor.MoveOperation.End)
    refresh(app)
    assert not editor.completer.popup().isVisible()


def test_empty_and_legacy_resource_sheets(assistant, app):
    assistant.window.plan.resources.clear()
    editor = assistant.prompt
    QTest.keyClicks(editor, "@")
    refresh(app)
    assert not editor.completer.popup().isVisible()
    assistant.window.plan.tasks[0].resource = "Legacy person"
    QTest.keyClicks(editor, "leg")
    refresh(app)
    assert choices(editor) == ["Legacy person"]
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText() == '@"Legacy person"'


def test_mention_in_middle_of_unicode_text_preserves_suffix(assistant, app):
    editor = assistant.prompt
    editor.setPlainText("😀 Assign @Craig tomorrow")
    cursor = editor.textCursor()
    prefix = "😀 Assign @Cr"
    cursor.setPosition(len(prefix.encode("utf-16-le")) // 2)
    editor.setTextCursor(cursor)
    refresh(app)
    assert editor.completer.popup().isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Return)
    refresh(app)
    assert editor.toPlainText() == "😀 Assign @Craig tomorrow"


def test_resource_names_refresh_and_use_staged_candidate(assistant, app):
    editor = assistant.prompt
    assistant.window.plan.resources.append(Resource(5, "New person"))
    QTest.keyClicks(editor, "@new")
    refresh(app)
    assert choices(editor) == ["New person"]
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    after = copy.deepcopy(assistant.window.plan)
    after.resources.append(Resource(6, "Staged person"))
    assistant.window.show_ai_preview(assistant.window.plan, after)
    assistant.window.activateWindow()
    editor.setFocus()
    refresh(app)
    editor.setPlainText("@sta")
    editor.moveCursor(QTextCursor.MoveOperation.End)
    refresh(app)
    assert choices(editor) == ["Staged person"]
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText() == '@"Staged person"'
    assistant.window.clear_ai_preview()


def test_selected_mention_is_sent_as_plain_prompt(assistant, app, monkeypatch):
    config = normalize_config("https://example.openai.azure.com", "model")
    assistant.window.preferences.setValue("ai/endpoint", config.endpoint)
    assistant.window.preferences.setValue("ai/deployment", config.deployment)
    assistant.session_endpoint, assistant.session_key = config.endpoint, "secret"
    assistant.session_provider = "foundry"
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    received = []
    def provider(config, key, snapshot, prompt, **kwargs):
        received.append((prompt, snapshot.payload))
        return AIResult("OK", True, None, None)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    QTest.keyClicks(assistant.prompt, "Assign @cr")
    refresh(app)
    QTest.keyClick(assistant.prompt, Qt.Key.Key_Return)
    assistant.start_request()
    assistant._thread.join(3)
    refresh(app)
    assert received[0][0] == "Assign @Craig"
    assert any(resource["name"] == "Craig" for resource in received[0][1]["resources"])
