"""Session-based AI dock, settings, and data-sharing disclosure."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget)

from .diff import compare_plans
from .proposals import ProposalError, make_snapshot, plan_revision
from .provider import (DEFAULT_BASE_PROMPT, EFFORTS, MAX_BASE_PROMPT, CredentialStore, ProviderError, discover_models,
                       normalize_config, request_proposal, validate_api_key)


def _model_key(endpoint):
    return "ai/models/" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _models(preferences, config):
    try:
        names = json.loads(preferences.value(_model_key(config.endpoint), "[]", type=str))
    except (ValueError, TypeError):
        names = []
    if not isinstance(names, list):
        names = []
    valid = []
    for name in [config.deployment, *names[:500]]:
        try:
            name = normalize_config(config.endpoint, name).deployment
        except ProviderError:
            continue
        if name not in valid:
            valid.append(name)
    return valid


def _icon(name):
    return QIcon(str(Path(__file__).resolve().parent.parent / "assets" / "ui" / f"ai_{name}.svg"))


class _StatusLabel(QLabel):
    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))


def _readable_placeholder(widget):
    palette = widget.palette()
    color = "#aeb8c2" if palette.color(QPalette.ColorRole.Base).lightness() < 128 else "#657584"
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(color))
    widget.setPalette(palette)


class AISettingsDialog(QDialog):
    forgotten = Signal(str)

    def __init__(self, preferences, credentials, parent=None):
        super().__init__(parent)
        self.preferences, self.credentials = preferences, credentials
        self.session_key, self.config = "", None
        self.setWindowTitle("AI settings")
        self.resize(650, 450)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.endpoint = QLineEdit(preferences.value("ai/endpoint", "", type=str))
        self.endpoint.setPlaceholderText("https://YOUR-RESOURCE.openai.azure.com/openai/v1/")
        self.deployment = QLineEdit(preferences.value("ai/deployment", "", type=str))
        self.deployment.setPlaceholderText("Your deployed model name")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Leave blank to keep an existing key")
        for field in (self.endpoint, self.deployment, self.key):
            field.setMinimumHeight(field.sizeHint().height())
            _readable_placeholder(field)
        self.remember = QCheckBox("Remember the key in the operating-system credential store")
        form.addRow("Foundry endpoint", self.endpoint)
        form.addRow("Deployment", self.deployment)
        form.addRow("API key", self.key)
        form.addRow("", self.remember)
        layout.addLayout(form)
        layout.addWidget(QLabel("Additional deployment names (one per line):"))
        self.deployments = QPlainTextEdit()
        self.deployments.setMaximumHeight(80)
        try:
            initial = normalize_config(self.endpoint.text(), self.deployment.text())
            self.deployments.setPlainText("\n".join(_models(preferences, initial)))
        except ProviderError:
            pass
        layout.addWidget(self.deployments)
        note = QLabel("API-key authentication only. A new key is session-only unless remembered. "
                      "An existing saved key remains until forgotten.")
        note.setWordWrap(True)
        layout.addWidget(note)
        forget = QPushButton("Forget saved key")
        forget.clicked.connect(self.forget_key)
        layout.addWidget(forget, alignment=Qt.AlignmentFlag.AlignLeft)
        label = QLabel("Base planning prompt (appended after fixed safety instructions)")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.base_prompt = QPlainTextEdit(preferences.value("ai/base_prompt", DEFAULT_BASE_PROMPT, type=str))
        self.base_prompt.setAccessibleName("Base planning prompt")
        layout.addWidget(self.base_prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def forget_key(self):
        try:
            config = normalize_config(self.endpoint.text(), self.deployment.text())
            if QMessageBox.question(self, "Forget API key?", "Remove the saved key for this Foundry resource?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.credentials.delete(config)
            self.key.clear()
            self.forgotten.emit(config.endpoint)
            QMessageBox.information(self, "API key removed", "The saved key was removed, if one existed.")
        except ProviderError as exc:
            QMessageBox.warning(self, "Cannot remove key", str(exc))

    def accept(self):
        try:
            config = normalize_config(self.endpoint.text(), self.deployment.text())
            key = self.key.text().strip()
            if key:
                key = validate_api_key(key)
            base_prompt = self.base_prompt.toPlainText()
            if len(base_prompt) > MAX_BASE_PROMPT:
                raise ProviderError("The base AI prompt must be at most 4,000 characters.")
            names = [config.deployment]
            for name in self.deployments.toPlainText().splitlines():
                if name.strip():
                    name = normalize_config(config.endpoint, name).deployment
                    if name not in names:
                        names.append(name)
            if len(names) > 500:
                raise ProviderError("At most 500 deployment names can be saved.")
            if key and self.remember.isChecked():
                self.credentials.set(config, key)
            self.preferences.setValue("ai/endpoint", config.endpoint)
            self.preferences.setValue("ai/deployment", config.deployment)
            self.preferences.setValue(_model_key(config.endpoint), json.dumps(names))
            self.preferences.setValue("ai/base_prompt", base_prompt)
            self.preferences.sync()
            if self.preferences.status() != QSettings.Status.NoError:
                raise ProviderError("Could not save AI settings in user preferences.")
            self.config, self.session_key = config, key
            self.key.clear()
            super().accept()
        except ProviderError as exc:
            QMessageBox.warning(self, "AI settings", str(exc))


class ConsentDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI data sharing")
        self.resize(560, 220)
        layout = QVBoxLayout(self)
        note = QLabel(f"Requests to {config.endpoint} (deployment {config.deployment}) send the project snapshot, "
                      "including names, resources, assignments and dates, your base prompt, request and session "
                      "conversation. Refinements include the staged proposal. No project files are attached. "
                      "The service may charge and apply its own retention or monitoring policies. Your API key "
                      "authenticates the connection but is not part of the model prompt. This disclosure is "
                      "shown once for this endpoint and deployment.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Agree and send")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class _WorkerSignals(QObject):
    finished = Signal(int, object, str)
    discovered = Signal(str, object, str)


class AIAssistantDock(QDockWidget):
    def __init__(self, window):
        super().__init__("AI project assistant", window)
        self.setObjectName("ai_assistant_dock")
        self.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.setTitleBarWidget(QWidget())
        self.window = window
        self.credentials = CredentialStore()
        self.session_endpoint = self.session_key = ""
        self._signals = _WorkerSignals(self)
        self._signals.finished.connect(self._finished)
        self._signals.discovered.connect(self._models_discovered)
        self._request_id = 0
        self._busy = self._cancelled = False
        self._thread = None
        self._snapshot = self._source_plan = self._result = None
        self._conversation_source = self._conversation_revision = None
        self._pending_prompt = ""
        self.auto_accept = False
        self._pending_auto = False
        self._discovering = False
        self._discovery_thread = None
        self._discovered = []
        self._discovered_endpoint = ""
        self.effort = None
        self.history = []
        self._usage = {"input": None, "output": None, "cached": None}

        content = QWidget()
        content.setStyleSheet("""
            QWidget { color: #24354a; background: #f5f7fa; }
            QPlainTextEdit { color: #24354a; background: #ffffff; border: 1px solid #b9c7d2; border-radius: 3px; padding: 4px; }
            QPushButton { color: #17334a; background: #e8f0f6; border: 1px solid #a3b3bf; border-radius: 4px; padding: 5px; }
            QPushButton:hover { background: #d2e5f3; }
            QPushButton:disabled { color: #81909d; background: #e3e8ec; }
        """)
        layout = QVBoxLayout(content)
        self.usage = QLabel("Tokens: not reported")
        self.usage.setWordWrap(True)
        layout.addWidget(self.usage)
        self.response = QPlainTextEdit()
        self.response.setReadOnly(True)
        self.response.setAccessibleName("AI session conversation")
        layout.addWidget(self.response, 1)
        self.status = _StatusLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.hide()
        layout.addWidget(self.status)
        self.changes = QPlainTextEdit()
        self.changes.setReadOnly(True)
        self.changes.setAccessibleName("AI proposal change details")
        self.changes.setMaximumHeight(125)
        self.changes.hide()
        layout.addWidget(self.changes)
        proposal_actions = QHBoxLayout()
        self.discard = QPushButton("Discard preview")
        self.discard.setEnabled(False)
        self.discard.clicked.connect(self.discard_preview)
        self.accept = QPushButton("Accept changes")
        self.accept.setEnabled(False)
        self.accept.clicked.connect(self.accept_changes)
        proposal_actions.addWidget(self.discard)
        proposal_actions.addWidget(self.accept)
        layout.addLayout(proposal_actions)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Ask for a change or refine the preview…")
        self.prompt.setAccessibleName("Ask AI")
        self.prompt.setMaximumHeight(110)
        _readable_placeholder(self.prompt)
        layout.addWidget(self.prompt)
        controls = QHBoxLayout()
        self.settings_button = QToolButton()
        self.settings_button.setIcon(QIcon(str(Path(__file__).resolve().parent.parent / "assets" / "flat_color_icons" / "settings.svg")))
        self.settings_button.setAccessibleName("AI settings")
        self.settings_button.setToolTip("AI settings")
        self.settings_button.clicked.connect(self.open_settings)
        self.shield = QToolButton()
        self.shield.setIcon(_icon("shield"))
        self.shield.clicked.connect(self.toggle_mode)
        self.clear = QToolButton()
        self.clear.setIcon(_icon("clear"))
        self.clear.setAccessibleName("Clear session")
        self.clear.setToolTip("Clear session")
        self.clear.clicked.connect(self.clear_session)
        for button in (self.settings_button, self.shield, self.clear):
            button.setIconSize(QSize(20, 20))
            controls.addWidget(button)
        controls.addStretch()
        self.model_button = QToolButton()
        self.model_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.model_button.setAccessibleName("Select model deployment")
        self.model_menu = QMenu(self.model_button)
        self.model_menu.aboutToShow.connect(self._populate_models)
        self.model_button.setMenu(self.model_menu)
        controls.addWidget(self.model_button)
        self.effort_button = QToolButton()
        self.effort_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.effort_button.setAccessibleName("Select reasoning effort")
        effort_menu = QMenu(self.effort_button)
        for effort in (None, *EFFORTS):
            action = effort_menu.addAction(effort or "Model default")
            action.triggered.connect(lambda checked=False, value=effort: self.select_effort(value))
        self.effort_button.setMenu(effort_menu)
        controls.addWidget(self.effort_button)
        self.send = QToolButton()
        self.send.setIconSize(QSize(20, 20))
        self.send.clicked.connect(self._send_or_stop)
        controls.addWidget(self.send)
        layout.addLayout(controls)
        self.setWidget(content)
        self._update_controls()
        self._set_buttons()

    def _append(self, who, text):
        self.response.appendPlainText(f"{who}:\n{text}\n")

    def _update_controls(self):
        model = self.window.preferences.value("ai/deployment", "", type=str)
        self.model_button.setText(self.model_button.fontMetrics().elidedText(model or "Choose model", Qt.TextElideMode.ElideRight, 110))
        self.model_button.setToolTip(f"Select a Foundry deployment: {model}" if model else "Select a Foundry deployment")
        self.effort_button.setText(self.effort or "default")
        self.effort_button.setToolTip("Reasoning effort (availability varies by model)")
        mode = "Auto-accept" if self.auto_accept else "Ask before applying"
        self.shield.setToolTip(f"{mode} — click to change")
        self.shield.setAccessibleName(f"Change permission mode: {mode}")
        self.shield.setStyleSheet("background: #ffe4be; border-radius: 4px;" if self.auto_accept else "")
        self.send.setIcon(_icon("stop" if self._busy else "play"))
        self.send.setAccessibleName("Stop request" if self._busy else "Send request")
        self.send.setToolTip("Stop request (the service may still charge)" if self._busy else "Send request")

    def _send_or_stop(self):
        if self._busy:
            self.cancel_request()
        else:
            self.start_request()

    def toggle_mode(self):
        if self._busy:
            return
        self.auto_accept = not self.auto_accept
        self._update_controls()
        self.status.setText("Auto-accept enabled for future requests." if self.auto_accept else "Ask before applying enabled.")
        if self.auto_accept and self.window.ai_preview is not None:
            self.status.setText("The current preview still requires acceptance or discard; auto-accept starts afterwards.")

    def select_effort(self, value):
        if not self._busy:
            self.effort = value
            self._update_controls()

    def _populate_models(self):
        menu = self.model_menu
        menu.clear()
        try:
            config = normalize_config(self.window.preferences.value("ai/endpoint", "", type=str),
                                      self.window.preferences.value("ai/deployment", "", type=str))
            names = _models(self.window.preferences, config)
            for name in names:
                action = menu.addAction(name)
                action.setCheckable(True)
                action.setChecked(name == config.deployment)
                action.triggered.connect(lambda checked=False, value=name: self.select_model(value))
            if self._discovered_endpoint == config.endpoint:
                new = [name for name in self._discovered if name not in names]
                if new:
                    menu.addSection("Foundry models — verify deployment")
                    for name in new:
                        action = menu.addAction(name)
                        action.triggered.connect(lambda checked=False, value=name: self.select_model(value))
            menu.addSeparator()
            refresh = menu.addAction("Discover models…")
            refresh.setEnabled(not self._discovering)
            refresh.triggered.connect(self.refresh_models)
        except ProviderError:
            pass
        menu.addAction("Manage deployments…").triggered.connect(self.open_settings)

    def select_model(self, name):
        if self._busy:
            return
        try:
            config = normalize_config(self.window.preferences.value("ai/endpoint", "", type=str), name)
            previous = self.window.preferences.value("ai/deployment", "", type=str)
            try:
                names = _models(self.window.preferences, normalize_config(config.endpoint, previous))
            except ProviderError:
                names = []
            if config.deployment not in names:
                names.append(config.deployment)
            self.window.preferences.setValue(_model_key(config.endpoint), json.dumps(names))
            self.window.preferences.setValue("ai/deployment", config.deployment)
            self.window.preferences.sync()
            if self.window.preferences.status() != QSettings.Status.NoError:
                raise ProviderError("Could not save the selected deployment.")
            self.effort = None
            self._update_controls()
        except ProviderError as exc:
            self.status.setText(str(exc))

    def refresh_models(self):
        if self._discovering or self._busy:
            return
        try:
            config = normalize_config(self.window.preferences.value("ai/endpoint", "", type=str),
                                      self.window.preferences.value("ai/deployment", "", type=str))
            key = self.session_key if self.session_endpoint == config.endpoint else ""
            key = validate_api_key(key or self.credentials.get(config) or "")
        except ProviderError as exc:
            self.status.setText(str(exc))
            return
        self._discovering = True
        self.status.setText("Discovering models…")
        signals = self._signals
        def work():
            try:
                signals.discovered.emit(config.endpoint, discover_models(config, key), "")
            except Exception:
                try:
                    signals.discovered.emit(config.endpoint, [], "Discovery failed. Add deployment names in AI settings.")
                except RuntimeError:
                    pass
        self._discovery_thread = threading.Thread(target=work, daemon=True, name="onepyproject-ai-models")
        self._discovery_thread.start()

    def _models_discovered(self, endpoint, names, error):
        self._discovering = False
        if error:
            self.status.setText(error)
            return
        self._discovered_endpoint, self._discovered = endpoint, names
        self.status.setText("Models loaded. Open the model menu; verify the names are deployed." if names else
                            "No models returned. Add deployment names in AI settings.")

    def open_settings(self):
        dialog = AISettingsDialog(self.window.preferences, self.credentials, self)
        dialog.forgotten.connect(self._forget_session)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                if self.session_endpoint != dialog.config.endpoint:
                    self.session_key = ""
                self.session_endpoint = dialog.config.endpoint
                if dialog.session_key:
                    self.session_key = dialog.session_key
                self.effort = None
                self._update_controls()
                self.status.setText("AI settings saved.")
        finally:
            dialog.key.clear()
            dialog.session_key = ""
            dialog.deleteLater()

    def _forget_session(self, endpoint):
        if self.session_endpoint == endpoint:
            self.session_key = ""

    def _fresh(self):
        try:
            return (self._snapshot is not None and self.window.plan is self._source_plan
                    and plan_revision(self.window.plan) == self._snapshot.revision
                    and plan_revision(self._snapshot.plan) == self._snapshot.revision)
        except ProposalError:
            return False

    def _set_buttons(self):
        staged = self._result is not None and self.window.ai_preview is not None
        self.send.setEnabled(not self._busy or not self._cancelled)
        for button in (self.settings_button, self.shield, self.model_button, self.effort_button):
            button.setEnabled(not self._busy)
        self.accept.setEnabled(staged and not self._busy)
        self.discard.setEnabled(staged and not self._busy)
        self.accept.setVisible(staged)
        self.discard.setVisible(staged)
        self.clear.setEnabled(not self._busy)
        self._update_controls()

    @staticmethod
    def _consent_key(config):
        identity = (config.endpoint + "\n" + config.deployment).encode("utf-8")
        return "ai/disclosure/" + hashlib.sha256(identity).hexdigest()

    def start_request(self):
        if self._busy:
            return
        if self.window.tree.state() == QAbstractItemView.State.EditingState:
            self.status.setText("Finish editing the task before asking AI.")
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt or len(prompt) > 4000:
            self.status.setText("Enter a request of at most 4,000 characters.")
            return
        try:
            config = normalize_config(self.window.preferences.value("ai/endpoint", "", type=str),
                                      self.window.preferences.value("ai/deployment", "", type=str))
            key = self.session_key if self.session_endpoint == config.endpoint else ""
            if not key:
                key = self.credentials.get(config) or ""
            key = validate_api_key(key)
            if (self._conversation_source is not None and
                    (self.window.plan is not self._conversation_source or
                     plan_revision(self.window.plan) != self._conversation_revision)):
                if self.window.ai_preview is not None:
                    self.discard_preview()
                self._reset_conversation()
            source = self.window.plan
            baseline = self._snapshot if self._fresh() else make_snapshot(source)
            if self._result is not None:
                if plan_revision(self._result.candidate) != self._result.candidate_revision:
                    raise ProposalError("The staged proposal changed unexpectedly; discard it and try again.")
                snapshot = make_snapshot(self._result.candidate)
            else:
                snapshot = baseline
            base_prompt = self.window.preferences.value("ai/base_prompt", DEFAULT_BASE_PROMPT, type=str)
        except (ProviderError, ProposalError, ValueError, OverflowError, RecursionError) as exc:
            self.status.setText(str(exc))
            return
        consent_key = self._consent_key(config)
        if not self.window.preferences.value(consent_key, False, type=bool):
            if ConsentDialog(config, self).exec() != QDialog.DialogCode.Accepted:
                return
            if self.window.plan is not source or plan_revision(source) != baseline.revision:
                self.status.setText("The project changed; request a new proposal.")
                return
            self.window.preferences.setValue(consent_key, True)
            self.window.preferences.sync()
            if self.window.preferences.status() != QSettings.Status.NoError:
                self.status.setText("Could not save the data-sharing consent; no data was sent.")
                return
        self._snapshot, self._source_plan = baseline, source
        self._conversation_source, self._conversation_revision = source, baseline.revision
        self._request_id += 1
        request_id = self._request_id
        self._busy, self._cancelled = True, False
        self._pending_auto = self.auto_accept and self.window.ai_preview is None
        self._pending_prompt = prompt
        self._append("You", prompt)
        self.prompt.clear()
        self._set_buttons()
        self.status.setText("Waiting for the model; the working plan has not changed.")
        signals = self._signals
        history = [dict(entry) for entry in self.history]
        reserved_tasks = [task.uid for task in baseline.plan.tasks]
        reserved_resources = [resource.uid for resource in baseline.plan.resources]
        effort = self.effort

        def work():
            try:
                result = request_proposal(config, key, snapshot, prompt, history=history, base_prompt=base_prompt,
                                          reserved_task_ids=reserved_tasks, reserved_resource_ids=reserved_resources,
                                          effort=effort)
                signals.finished.emit(request_id, result, "")
            except (ProviderError, ProposalError) as exc:
                try:
                    signals.finished.emit(request_id, None, str(exc))
                except RuntimeError:
                    pass
            except Exception:
                try:
                    signals.finished.emit(request_id, None, "The AI request failed; no changes were applied.")
                except RuntimeError:
                    pass
        self._thread = threading.Thread(target=work, daemon=True, name="onepyproject-ai")
        self._thread.start()

    def cancel_request(self):
        if self._busy:
            self._cancelled = True
            self._set_buttons()
            self.status.setText("Cancelled locally. The request may still complete and incur service charges.")

    def _record_usage(self, result):
        values = {"input": result.prompt_tokens, "output": result.completion_tokens,
                  "cached": result.cached_prompt_tokens}
        def describe(items):
            return ", ".join(f"{name} {value if value is not None else 'not reported'}" for name, value in items.items())
        for name, value in values.items():
            if type(value) is int and value >= 0:
                self._usage[name] = (self._usage[name] or 0) + value
        self.usage.setText(f"Last: {describe(values)}\nSession reported totals: {describe(self._usage)}")

    def _finished(self, request_id, result, error):
        if request_id != self._request_id:
            return
        self._busy = False
        if result is not None:
            self._record_usage(result)
        if self._cancelled:
            self._append("System", "Request cancelled; no changes were applied.")
            self.status.setText("Request cancelled; any earlier preview remains available.")
        elif error:
            self._append("System", error)
            self.status.setText(error)
        elif not self._fresh():
            self.discard_preview()
            self.status.setText("The project changed while the model was working. Request a new proposal.")
        elif self.window.tree.state() == QAbstractItemView.State.EditingState:
            self.status.setText("A task is being edited. Finish the edit and request a new proposal.")
        elif QApplication.activeModalWidget() is not None:
            self.status.setText("A dialog is open. Finish it and request a new proposal.")
        else:
            try:
                valid_candidate = (result.candidate is None or (result.candidate_revision is not None and
                                   plan_revision(result.candidate) == result.candidate_revision))
            except ProposalError:
                valid_candidate = False
            if not valid_candidate:
                self.status.setText("The proposal changed unexpectedly; the earlier preview was kept.")
            else:
                self.history.extend([{"role": "user", "content": self._pending_prompt},
                                     {"role": "assistant", "content": result.message}])
                self._append("AI", result.message)
                if result.candidate is not None:
                    try:
                        diff = compare_plans(self._snapshot.plan, result.candidate)
                        if diff.changes:
                            if self._pending_auto:
                                self.window.apply_ai_candidate(self._source_plan, self._snapshot.revision,
                                                               result.candidate, result.candidate_revision)
                                self._snapshot = self._source_plan = None
                                self._conversation_source = self.window.plan
                                self._conversation_revision = plan_revision(self.window.plan)
                                self._append("System", "Changes automatically applied as one undoable action. Save when ready.")
                                self.status.setText("Changes automatically applied. Use Undo to revert.")
                            else:
                                self.window.show_ai_preview(self._snapshot.plan, result.candidate)
                                self._result = result
                                self.changes.setPlainText("\n".join(
                                    f"{c.category} · {c.item} · {c.field}: {c.before} → {c.after}" for c in diff.changes))
                                self.changes.show()
                                self.status.setText("Preview ready. Scroll to review, refine, accept or discard.")
                        else:
                            self.discard_preview()
                            self.status.setText("The refined proposal matches the working plan; no changes remain.")
                    except Exception:
                        self.status.setText("Could not apply the proposal; no changes were kept." if self._pending_auto else
                                            "Could not display the proposal; the earlier preview was kept.")
                else:
                    self.status.setText("No new changes proposed; any earlier preview remains available.")
        self._pending_prompt = ""
        self._set_buttons()

    def discard_preview(self):
        had_preview = self._result is not None
        self.window.clear_ai_preview()
        self._result = None
        self.changes.clear()
        self.changes.hide()
        self._set_buttons()
        self.status.setText("Preview discarded; the working plan is unchanged.")
        if had_preview:
            self._append("System", "The preview was discarded; the working plan is unchanged.")
            self.history.append({"role": "assistant", "content": "The previous preview was discarded; the working plan is unchanged."})

    def accept_changes(self):
        if self._busy or self._result is None:
            return
        try:
            valid = self._fresh() and plan_revision(self._result.candidate) == self._result.candidate_revision
        except ProposalError:
            valid = False
        if not valid:
            self.discard_preview()
            self.status.setText("The project or proposal changed; request a new proposal.")
            return
        candidate, revision = self._result.candidate, self._result.candidate_revision
        self.window.clear_ai_preview()
        try:
            self.window.apply_ai_candidate(self._source_plan, self._snapshot.revision, candidate, revision)
        except (ProposalError, ValueError, OverflowError, RecursionError) as exc:
            self._result = None
            self._set_buttons()
            self.status.setText(str(exc))
            return
        self._result = None
        self._snapshot = self._source_plan = None
        self._conversation_source, self._conversation_revision = self.window.plan, plan_revision(self.window.plan)
        self.changes.clear()
        self.changes.hide()
        self._append("System", "Changes accepted as one undoable action.")
        self.status.setText("Changes applied. Save the project when ready.")
        self._set_buttons()

    def _reset_conversation(self):
        self.history.clear()
        self.response.clear()
        self._usage = {"input": None, "output": None, "cached": None}
        self.usage.setText("Tokens: not reported")
        self._snapshot = self._source_plan = None
        self._conversation_source = self._conversation_revision = None
        self.auto_accept = False
        self._update_controls()

    def clear_session(self):
        if self._busy:
            return
        self.discard_preview()
        self._reset_conversation()
        self.prompt.clear()
        self.status.setText("AI session cleared.")

    def closeEvent(self, event):
        super().closeEvent(event)
