import copy
import threading

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog, QDockWidget, QLineEdit, QMessageBox

from onepyproject.ai.diff import compare_plans
from onepyproject.ai.preview import ADDED, CHANGED, CHANGED_CELL, REMOVED
from onepyproject.ai.proposals import FIELDS, ProposalError, apply_operations, make_snapshot, plan_revision
from onepyproject.ai.provider import AIResult, ProviderError, normalize_config
from onepyproject.ai.ui import AIAssistantDock, AISettingsDialog, ConsentDialog
from onepyproject.app import MainWindow
from onepyproject.model import Plan, Task


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    preferences = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", tasks=[Task(10, "Design", duration=5), Task(20, "Test", predecessors=[10])])
    window.reset_history(saved=True)
    window.refresh()
    yield window
    window.dirty = False
    window.close()


class FakeCredentials:
    def __init__(self):
        self.values = {}
        self.saved = []
    def set(self, config, key):
        self.values[config.endpoint] = key
        self.saved.append((config.endpoint, key))
    def get(self, config):
        return self.values.get(config.endpoint)
    def delete(self, config):
        self.values.pop(config.endpoint, None)


def candidate_for(plan, duration=3):
    snapshot = make_snapshot(plan)
    op = {name: None for name in FIELDS}
    op.update(op="update_task", target="task:10", duration_days=duration)
    proposal = {"schema_version": "1", "snapshot_revision": snapshot.revision, "message": "Shorten design",
                "needs_clarification": False, "operations": [op]}
    candidate = apply_operations(snapshot, proposal)
    return snapshot, candidate, compare_plans(plan, candidate)


def configured(window):
    window.open_ai()
    assistant = window._ai_dialog
    config = normalize_config("https://example.openai.azure.com", "model")
    window.preferences.setValue("ai/endpoint", config.endpoint)
    window.preferences.setValue("ai/deployment", config.deployment)
    assistant.session_endpoint, assistant.session_key = config.endpoint, "secret"
    assistant.session_provider = "foundry"
    return assistant, config


def finish(assistant, app):
    assistant._thread.join(3)
    assert not assistant._thread.is_alive()
    app.processEvents()


def fake_result(snapshot, duration=3, message="Shorten design"):
    _, candidate, diff = candidate_for(snapshot.plan, duration)
    return AIResult(message, False, candidate, diff, 100, 25, plan_revision(candidate), 40)


@pytest.mark.parametrize("remember", [False, True])
def test_ai_settings_store_prompt_but_never_key_in_preferences(app, tmp_path, remember):
    path = tmp_path / "settings.ini"
    preferences = QSettings(str(path), QSettings.Format.IniFormat)
    credentials = FakeCredentials()
    dialog = AISettingsDialog(preferences, credentials)
    assert dialog.key.echoMode() == QLineEdit.EchoMode.Password
    dialog.show()
    app.processEvents()
    assert all(field.height() >= field.fontMetrics().height() + 2 for field in (dialog.endpoint, dialog.deployment, dialog.key))
    dialog.endpoint.setText("https://example.openai.azure.com")
    dialog.deployment.setText("my-model")
    dialog.key.setText("private-test-key")
    dialog.base_prompt.setPlainText("Prefer short milestones")
    dialog.remember.setChecked(remember)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session_key == "private-test-key" and bool(credentials.saved) is remember
    assert "private-test-key" not in path.read_text(encoding="utf-8")
    assert preferences.value("ai/base_prompt") == "Prefer short milestones"


def test_key_storage_failure_and_invalid_key_never_save_preferences(app, tmp_path, monkeypatch):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    credentials = FakeCredentials()
    def fail(*args):
        raise ProviderError("Credential store unavailable")
    credentials.set = fail
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    dialog = AISettingsDialog(preferences, credentials)
    dialog.endpoint.setText("https://example.openai.azure.com")
    dialog.deployment.setText("model")
    dialog.key.setText("invalid key")
    dialog.accept()
    assert errors and not preferences.contains("ai/endpoint")
    dialog.key.setText("secret-test")
    dialog.remember.setChecked(True)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and len(errors) == 2
    assert not preferences.contains("ai/endpoint")
    assert not (tmp_path / "settings.ini").exists() or "secret-test" not in (tmp_path / "settings.ini").read_text()


def test_settings_saves_multiple_deployments_for_model_picker(app, tmp_path):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = AISettingsDialog(preferences, FakeCredentials())
    dialog.endpoint.setText("https://example.openai.azure.com")
    dialog.deployment.setText("primary")
    dialog.deployments.setPlainText("secondary\nthird\nsecondary")
    dialog.accept()
    reopened = AISettingsDialog(preferences, FakeCredentials())
    assert reopened.deployments.toPlainText().splitlines() == ["primary", "secondary", "third"]


def test_gemini_settings_save_provider_without_storing_access_tokens(app, tmp_path):
    path = tmp_path / "settings.ini"
    preferences = QSettings(str(path), QSettings.Format.IniFormat)
    dialog = AISettingsDialog(preferences, FakeCredentials())
    dialog.provider.setCurrentIndex(dialog.provider.findData("gemini"))
    dialog.project.setText("onepyproject-123")
    dialog.location.setText("global")
    dialog.gemini_model.setText("google/gemini-3.5-flash")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert preferences.value("ai/provider") == "gemini"
    assert preferences.value("ai/gemini/project") == "onepyproject-123"
    assert preferences.value("ai/gemini/location") == "global"
    assert preferences.value("ai/gemini/model") == "google/gemini-3.5-flash"
    assert "access-token" not in path.read_text(encoding="utf-8")
    assert not dialog.google_login.isHidden() and dialog.endpoint.isHidden()


def test_gemini_provider_routes_request_through_google_adapter(window, monkeypatch, app):
    window.preferences.setValue("ai/provider", "gemini")
    window.preferences.setValue("ai/gemini/project", "onepyproject-123")
    window.preferences.setValue("ai/gemini/location", "global")
    window.preferences.setValue("ai/gemini/model", "google/gemini-3.5-flash")
    window.open_ai()
    assistant = window._ai_dialog
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    calls = []
    def provider(config, snapshot, prompt, **kwargs):
        calls.append((config, prompt, kwargs))
        return AIResult("Need details", True, None, None)
    monkeypatch.setattr("onepyproject.ai.ui.request_gemini_proposal", provider)
    assistant.prompt.setPlainText("What is the next milestone?")
    assistant.start_request()
    finish(assistant, app)
    assert len(calls) == 1
    assert calls[0][0].project == "onepyproject-123" and calls[0][0].location == "global"
    assert calls[0][0].model == "google/gemini-3.5-flash"
    assert calls[0][1] == "What is the next milestone?"


def test_settings_clear_cancelled_key_and_forget_session(window, monkeypatch):
    assistant, config = configured(window)
    assistant.credentials = FakeCredentials()
    assistant.credentials.set(config, "stored-secret")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: QMessageBox.StandardButton.Ok)
    seen = []
    def forget(dialog):
        dialog.key.setText("secret-value")
        dialog.forget_key()
        seen.append(dialog)
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(AISettingsDialog, "exec", forget)
    assistant.open_settings()
    assert not seen[0].key.text() and not seen[0].session_key
    assert not assistant.session_key and assistant.credentials.get(config) is None


def test_ai_docks_right_and_preserves_conversation_when_hidden(window, app):
    window.show()
    window.ai_button.setFocus()
    QTest.keyClick(window.ai_button, Qt.Key.Key_Space)
    app.processEvents()
    assistant = window._ai_dialog
    assert isinstance(assistant, QDockWidget) and window.dockWidgetArea(assistant) == Qt.DockWidgetArea.RightDockWidgetArea
    assistant.response.setPlainText("Example conversation")
    assistant.close()
    window.open_ai()
    assert assistant.response.toPlainText() == "Example conversation"


def test_preview_colors_readonly_gantt_and_restore(window, app):
    window.show()
    app.processEvents()
    window.tree.setCurrentItem(window.items[10])
    baseline = copy.deepcopy(window.plan)
    candidate = copy.deepcopy(window.plan)
    candidate.tasks[0].duration = 3
    candidate.tasks.remove(candidate.tasks[1])
    candidate.tasks.append(Task(30, "Added"))
    window.show_ai_preview(window.plan, candidate)
    app.processEvents()
    assert window.plan == baseline and not window.dirty and not window._undo
    assert window.items[10].background(1).color().name() == CHANGED
    assert window.items[10].background(2).color().name() == CHANGED_CELL
    assert window.items[20].background(1).color().name() == REMOVED
    assert window.items[30].background(1).color().name() == ADDED
    assert not window.file_toolbar.isEnabled()
    assert all(not action.isEnabled() for action in window.task_toolbar.actions())
    assert window.ai_button.isEnabled()
    assert not window.items[10].flags() & Qt.ItemFlag.ItemIsEditable
    window.edit_cell(10, 1, "Not allowed")
    QTest.keyClick(window.tree, Qt.Key.Key_Delete)
    window.undo()
    assert window.plan == baseline and not window._undo
    assert not window.gantt.grab().isNull()
    window.clear_ai_preview()
    assert window.plan == baseline and window.selected_uid() == 10
    assert window.task_toolbar.isEnabled() and window.file_toolbar.isEnabled()
    assert window.items[10].flags() & Qt.ItemFlag.ItemIsEditable


def test_preview_restores_collapsed_outline_and_draws_both_schedules(window, app, monkeypatch):
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase"), Task(10, "Design", parent=1, duration=5)])
    window.refresh()
    window.show()
    window.items[1].setExpanded(False)
    after = copy.deepcopy(window.plan)
    after.tasks[1].duration = 2
    drawn = []
    original = window.gantt.draw_preview_bar
    def record(painter, item, y, proposed=False):
        drawn.append((item, proposed))
        return original(painter, item, y, proposed)
    monkeypatch.setattr(window.gantt, "draw_preview_bar", record)
    window.show_ai_preview(window.plan, after)
    app.processEvents()
    assert window.items[1].isExpanded()
    assert not window.gantt.grab().isNull()
    row = next(row for row in window.ai_preview.rows if row.uid == 10)
    assert (row.before, False) in drawn and (row.after, True) in drawn
    window.clear_ai_preview()
    assert not window.items[1].isExpanded()


def test_preview_tooltip_escapes_untrusted_names(window):
    after = copy.deepcopy(window.plan)
    after.tasks[0].name = '<img src="https://invalid.example">'
    window.show_ai_preview(window.plan, after)
    tooltip = window.items[10].toolTip(1)
    assert "&lt;img" in tooltip and "<img" not in tooltip
    window.clear_ai_preview()


def test_preview_gantt_spans_far_proposed_date(window):
    after = copy.deepcopy(window.plan)
    after.tasks[0].earliest = "2055-01-01"
    window.show_ai_preview(window.plan, after)
    row = next(row for row in window.ai_preview.rows if row.uid == 10)
    assert window.gantt.x(row.after.finish) < window.gantt.width()
    window.clear_ai_preview()


def test_apply_atomic_and_undo_preserves_unsaved_work(window, monkeypatch):
    window.edit_cell(10, 1, "Unsaved design")
    before = copy.deepcopy(window.plan)
    source = window.plan
    snapshot, candidate, _ = candidate_for(source)
    old_undo = len(window._undo)
    window.apply_ai_candidate(source, snapshot.revision, candidate, plan_revision(candidate))
    assert window.plan.tasks[0].duration == 3 and window.plan.tasks[0].name == "Unsaved design"
    assert window.dirty and len(window._undo) == old_undo + 1
    window.undo()
    assert window.plan == before and window.dirty
    window.redo()
    assert window.plan.tasks[0].duration == 3
    source = window.plan
    snapshot, candidate, _ = candidate_for(source, 4)
    revision = plan_revision(candidate)
    window.edit_cell(10, 1, "Changed while waiting")
    with pytest.raises(ProposalError, match="project changed"):
        window.apply_ai_candidate(source, snapshot.revision, candidate, revision)
    source = window.plan
    snapshot, candidate, _ = candidate_for(source, 4)
    revision = plan_revision(candidate)
    candidate.tasks[0].name = "Tampered"
    with pytest.raises(ProposalError, match="proposal changed"):
        window.apply_ai_candidate(source, snapshot.revision, candidate, revision)


def test_apply_restores_plan_and_undo_if_refresh_fails(window, monkeypatch):
    source = window.plan
    snapshot, candidate, _ = candidate_for(source)
    old = copy.deepcopy(source)
    refresh = window.refresh
    attempts = []
    def flaky(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("Simulated UI error")
        return refresh(*args)
    monkeypatch.setattr(window, "refresh", flaky)
    with pytest.raises(ProposalError, match="restored"):
        window.apply_ai_candidate(source, snapshot.revision, candidate, plan_revision(candidate))
    assert window.plan == old and not window.dirty and not window._undo and not window._redo


def test_declined_disclosure_never_sends(window, monkeypatch):
    assistant, _ = configured(window)
    assistant.prompt.setPlainText("Shorten Design")
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    def unexpected(*args, **kwargs):
        raise AssertionError("A declined request must not go to the provider")
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", unexpected)
    assistant.start_request()
    assert not assistant._busy and not window.dirty and not window._undo


def test_session_refinement_preview_consent_usage_and_accept(window, monkeypatch, app):
    assistant, config = configured(window)
    window.preferences.setValue("ai/base_prompt", "Use milestones")
    disclosures, requests = [], []
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: disclosures.append(True) or QDialog.DialogCode.Accepted)
    def provider(config, key, snapshot, prompt, **kwargs):
        requests.append((copy.deepcopy(snapshot.plan), prompt, kwargs))
        return fake_result(snapshot, 3 if len(requests) == 1 else 2)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Shorten design")
    assistant.start_request()
    finish(assistant, app)
    assert assistant.accept.isEnabled() and window.plan.tasks[0].duration == 5 and not window.dirty
    assert window.ai_preview.after.tasks[0].duration == 3
    assert "cached 40" in assistant.usage.text() and "You:" in assistant.response.toPlainText()
    assistant.prompt.setPlainText("Even shorter")
    assistant.start_request()
    finish(assistant, app)
    assert len(disclosures) == 1 and requests[1][0].tasks[0].duration == 3
    assert requests[1][2]["history"] == [{"role": "user", "content": "Shorten design"},
                                         {"role": "assistant", "content": "Shorten design"}]
    assert requests[1][2]["base_prompt"] == "Use milestones"
    assert 10 in requests[1][2]["reserved_task_ids"]
    assert window.ai_preview.before.tasks[0].duration == 5 and window.ai_preview.after.tasks[0].duration == 2
    assert "cached 80" in assistant.usage.text()
    assistant.accept.click()
    assert window.ai_preview is None and window.plan.tasks[0].duration == 2 and len(window._undo) == 1
    window.undo()
    assert window.plan.tasks[0].duration == 5 and not window.dirty
    assert "Even shorter" in assistant.response.toPlainText()


def test_generated_duration_is_unmarked_in_preview_and_after_accept(window, monkeypatch, app):
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    def provider(config, key, snapshot, prompt, **kwargs):
        operation = {field: None for field in FIELDS}
        operation.update(op="update_task", target="task:10", duration_days=3, estimated=True)
        proposal = {"schema_version": "1", "snapshot_revision": snapshot.revision, "message": "Updated",
                    "needs_clarification": False, "operations": [operation]}
        candidate = apply_operations(snapshot, proposal)
        return AIResult("Updated", False, candidate, compare_plans(snapshot.plan, candidate),
                        candidate_revision=plan_revision(candidate))
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Generate duration")
    assistant.start_request()
    finish(assistant, app)
    assert window.items[10].text(2) == "3 d"
    assistant.accept.click()
    assert window.items[10].text(2) == "3 d" and not window.plan.tasks[0].estimated


def test_auto_accept_applies_validated_candidate_with_undo(window, monkeypatch, app):
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", lambda config, key, snapshot, prompt, **kwargs: fake_result(snapshot))
    assistant.toggle_mode()
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    assert assistant.send.accessibleName() == "Stop request"
    finish(assistant, app)
    assert window.ai_preview is None and window.plan.tasks[0].duration == 3 and len(window._undo) == 1
    assert "automatically applied" in assistant.response.toPlainText()
    window.undo()
    assert window.plan.tasks[0].duration == 5
    assistant.clear_session()
    assert not assistant.auto_accept


def test_auto_mode_never_applies_an_existing_preview(window, monkeypatch, app):
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", lambda config, key, snapshot, prompt, **kwargs: fake_result(snapshot, 2))
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    finish(assistant, app)
    assistant.toggle_mode()
    assert window.plan.tasks[0].duration == 5 and window.ai_preview is not None
    assistant.prompt.setPlainText("Refine")
    assistant.start_request()
    finish(assistant, app)
    assert window.plan.tasks[0].duration == 5 and assistant.accept.isEnabled()
    assistant.discard_preview()


def test_model_and_effort_selection_are_sent_with_followup(window, monkeypatch, app):
    assistant, config = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    calls = []
    def provider(config, key, snapshot, prompt, **kwargs):
        calls.append((config.deployment, kwargs["effort"]))
        return AIResult("Need details", True, None, None)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.select_model("second-deployment")
    assistant.select_effort("high")
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    finish(assistant, app)
    assert calls == [("second-deployment", "high")]
    assistant.select_model(config.deployment)
    assert assistant.effort is None
    assistant._populate_models()
    assert "second-deployment" in [action.text() for action in assistant.model_menu.actions()]


def test_ai_composer_has_searchable_model_and_effort_selectors(window):
    assistant, config = configured(window)
    assistant.select_model("second-deployment")
    assistant.select_model(config.deployment)
    assistant._populate_models()

    assert assistant.prompt.parentWidget().objectName() == "ai_composer"
    assert "QPlainTextEdit#ai_prompt" in assistant.widget().styleSheet()
    assert assistant.response.objectName() == "ai_response"
    assert "QPlainTextEdit#ai_response" in assistant.widget().styleSheet()
    assert "::menu-indicator" in assistant.widget().styleSheet()
    assert assistant.model_search.placeholderText() == "Search models"
    choices = [action for action in assistant.model_menu.actions() if action.property("model_choice")]
    assistant.model_search.setText("second")
    assert [action.text() for action in choices if action.isVisible()] == ["second-deployment"]

    assistant._populate_efforts()
    assert [action.text() for action in assistant.effort_menu.actions() if action.isCheckable()] == [
        "Model default", "None", "Minimal", "Low", "Medium", "High", "Extra High"
    ]


def test_discovered_models_can_be_selected_and_are_saved(window, monkeypatch, app):
    assistant, config = configured(window)
    monkeypatch.setattr("onepyproject.ai.ui.discover_models", lambda config, key: ["discovered-model"])
    assistant.refresh_models()
    assistant._discovery_thread.join(3)
    app.processEvents()
    assistant._populate_models()
    assert "discovered-model" in [action.text() for action in assistant.model_menu.actions()]
    assistant.select_model("discovered-model")
    assistant._populate_models()
    names = [action.text() for action in assistant.model_menu.actions()]
    assert config.deployment in names and "discovered-model" in names


def test_cancel_in_auto_mode_does_not_apply_result(window, monkeypatch, app):
    assistant, _ = configured(window)
    assistant.toggle_mode()
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    release = threading.Event()
    def provider(config, key, snapshot, prompt, **kwargs):
        assert release.wait(5)
        return fake_result(snapshot)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    assistant.send.click()
    release.set()
    finish(assistant, app)
    assert not window.dirty and window.plan.tasks[0].duration == 5 and window.ai_preview is None


def test_dock_can_hide_and_reopen_during_preview(window, monkeypatch, app):
    window.show()
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", lambda config, key, snapshot, prompt, **kwargs: fake_result(snapshot))
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    finish(assistant, app)
    assistant.close()
    assert assistant.isHidden() and window.ai_preview is not None
    window.ai_button.click()
    assert not assistant.isHidden() and assistant.accept.isEnabled()
    assistant.discard.click()
    assert window.ai_preview is None and window.plan.tasks[0].duration == 5


def test_new_project_does_not_send_previous_project_conversation(window, monkeypatch, app):
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    calls = []
    def provider(config, key, snapshot, prompt, **kwargs):
        calls.append(kwargs["history"])
        return fake_result(snapshot)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Private previous request")
    assistant.start_request()
    finish(assistant, app)
    assistant.accept_changes()
    window.plan = copy.deepcopy(window.plan)
    window.plan.title = "Another project"
    window.refresh()
    assistant.prompt.setPlainText("New request")
    assistant.start_request()
    finish(assistant, app)
    assert calls == [[], []]
    assert "Private previous request" not in assistant.response.toPlainText()


def test_disclosure_is_once_per_endpoint_and_deployment(window, monkeypatch, app):
    assistant, config = configured(window)
    calls = []
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: calls.append(True) or QDialog.DialogCode.Accepted)
    def provider(config, key, snapshot, prompt, **kwargs):
        return AIResult("Need details", True, None, None)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    for deployment in (config.deployment, config.deployment, "other-model"):
        window.preferences.setValue("ai/deployment", deployment)
        assistant.prompt.setPlainText("Change")
        assistant.start_request()
        finish(assistant, app)
    assert len(calls) == 2
    assert "cached not reported" in assistant.usage.text()


@pytest.mark.parametrize("automatic", [False, True])
def test_changed_or_cancelled_background_response_never_stages(window, monkeypatch, app, automatic):
    assistant, _ = configured(window)
    if automatic:
        assistant.toggle_mode()
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    release = threading.Event()
    def provider(config, key, snapshot, prompt, **kwargs):
        assert release.wait(5)
        return fake_result(snapshot)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    window.edit_cell(10, 1, "Changed while waiting")
    release.set()
    finish(assistant, app)
    assert not assistant.accept.isEnabled() and window.ai_preview is None
    assert "changed" in assistant.status.text().lower()
    release.clear()
    assistant.prompt.setPlainText("Change again")
    assistant.start_request()
    assistant.cancel_request()
    release.set()
    finish(assistant, app)
    assert not assistant.accept.isEnabled() and window.ai_preview is None
    assert "cancelled" in assistant.status.text().lower()
    assert "cached 40" in assistant.usage.text()


def test_response_does_not_discard_an_active_cell_editor(window, monkeypatch, app):
    window.show()
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    release = threading.Event()
    def provider(config, key, snapshot, prompt, **kwargs):
        assert release.wait(5)
        return fake_result(snapshot)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    window.tree.setCurrentItem(window.items[10], 1)
    window.tree.editItem(window.items[10], 1)
    app.processEvents()
    assert window.tree.state() == QAbstractItemView.State.EditingState
    editor = window.tree.findChild(QLineEdit)
    editor.setText("Uncommitted text")
    release.set()
    finish(assistant, app)
    assert window.ai_preview is None and editor.text() == "Uncommitted text"
    assert "being edited" in assistant.status.text().lower()
    QTest.keyClick(editor, Qt.Key.Key_Escape)


def test_failed_refinement_keeps_previous_preview(window, monkeypatch, app):
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    calls = []
    def provider(config, key, snapshot, prompt, **kwargs):
        calls.append(prompt)
        if len(calls) > 1:
            raise ProviderError("Request failed")
        return fake_result(snapshot)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    finish(assistant, app)
    original = assistant._result
    assistant.prompt.setPlainText("Refine")
    assistant.start_request()
    finish(assistant, app)
    assert assistant._result is original and assistant.accept.isEnabled()
    assert window.ai_preview.after.tasks[0].duration == 3 and window.plan.tasks[0].duration == 5
    assistant.discard_preview()


def test_response_does_not_stage_while_another_dialog_is_open(window, monkeypatch, app):
    window.show()
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    release = threading.Event()
    def provider(config, key, snapshot, prompt, **kwargs):
        assert release.wait(5)
        return fake_result(snapshot)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", provider)
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    app.processEvents()
    assert QApplication.activeModalWidget() is dialog
    release.set()
    finish(assistant, app)
    assert window.ai_preview is None and "dialog is open" in assistant.status.text().lower()
    dialog.reject()


def test_tampered_candidate_is_never_applied(window, monkeypatch, app):
    assistant, _ = configured(window)
    monkeypatch.setattr(ConsentDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr("onepyproject.ai.ui.request_proposal", lambda config, key, snapshot, prompt, **kwargs: fake_result(snapshot))
    assistant.prompt.setPlainText("Change")
    assistant.start_request()
    finish(assistant, app)
    assistant._result.candidate.tasks[0].duration = 4
    assistant.accept_changes()
    assert window.plan.tasks[0].duration == 5 and not window.dirty and window.ai_preview is None
