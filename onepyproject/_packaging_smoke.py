"""Exercise packaged resources without using a user's settings or project files."""
import hashlib
import importlib.metadata
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QSettings, QSize, QTimer
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QComboBox

import pymppwriter
from . import __version__
from .app import ExportDialog, MainWindow, SettingsDialog, display_time
from .icons import APPLICATION_ICON_PATH, ASSET_DIR, ICON_FILES, application_icon
from .model import Plan, Resource, ResourceAssignment, Task, load_plan, save_plan
from .mpp import export_mpp, import_mpp
from .report import export_html
from .report_pdf import export_pdf
from .ai.diff import compare_plans
from .ai.proposals import FIELDS, apply_operations, make_snapshot
from .ai.provider import CredentialStore, normalize_config, request_proposal
from .clipboard import release_rows


def run():
    app = QApplication([])
    app.setApplicationName("OnePyProject packaging smoke test")
    if pymppwriter.__version__ != "0.4.3":
        raise RuntimeError(f"Unexpected pymppwriter version: {pymppwriter.__version__}")
    # The pinned fork reports the same version as the PyPI release, so check for what only the fork has:
    # the reader that skips stale records, and the writer step that places a lead before its predecessor.
    from pymppwriter import reader, writer
    if not hasattr(reader._Klass, "live_rows") or not hasattr(writer, "retreat_working"):
        raise RuntimeError("pymppwriter lacks the fixes OnePyProject needs; build against the pinned fork commit")

    # Bypass toolbar_icon's fallback so missing SVG plugins cannot pass silently.
    app_icon = application_icon()
    if not APPLICATION_ICON_PATH.is_file() or app_icon.pixmap(QSize(128, 128)).isNull():
        raise RuntimeError("Application icon failed to render")
    for filename in ICON_FILES.values():
        image = QIcon(str(ASSET_DIR / filename)).pixmap(QSize(20, 20), 2.0).toImage()
        if image.isNull() or not any(
            image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height())
        ):
            raise RuntimeError(f"SVG icon failed to render: {filename}")
    for filename in ("tree_open.svg", "tree_closed.svg", "ai_shield.svg", "ai_clear.svg", "ai_play.svg", "ai_stop.svg",
                     "summary_color.svg",
                     "cut.svg", "copy.svg", "paste.svg"):
        image = QImage(str(ASSET_DIR.parent / "ui" / filename))
        if image.isNull():
            raise RuntimeError(f"SVG image failed to load: {filename}")
    if QIcon(str(ASSET_DIR.parent / "ui" / "ai_assistant.svg")).pixmap(QSize(20, 20)).isNull():
        raise RuntimeError("AI icon failed to render")

    with TemporaryDirectory(prefix="onepyproject-smoke-") as directory:
        directory = Path(directory)
        plan = Plan(start="2026-11-16", resources=[Resource(1, "Example")],
                    tasks=[Task(10, "Parent"), Task(20, "Child", parent=10, assignments=[ResourceAssignment(1)],
                                                   percent_complete=50),
                           Task(30, "After", predecessors=[20])])
        path = directory / "smoke.pxplan"
        save_plan(plan, path)
        plan = load_plan(path)
        if [task.name for task in plan.tasks] != ["Parent", "Child", "After"]:
            raise RuntimeError("Plan did not survive a save/load round trip")
        if not all(importlib.metadata.version(name) for name in ("openai", "keyring", "google-auth", "requests")):
            raise RuntimeError("AI dependency metadata is missing")
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials as GoogleUserCredentials
        if not GoogleUserCredentials(token="offline-smoke-test").token or GoogleAuthRequest() is None:
            raise RuntimeError("Google SSO authentication dependencies are unavailable")
        normalize_config("https://example.openai.azure.com", "example")
        from openai import DefaultHttpxClient, OpenAI
        client = OpenAI(base_url="https://example.openai.azure.com/openai/v1/", api_key="offline-smoke-test",
                        http_client=DefaultHttpxClient(follow_redirects=False), max_retries=0)
        client.chat.completions
        client.close()
        if sys.platform in ("darwin", "win32"):
            CredentialStore._backend().priority
        snapshot = make_snapshot(plan)
        operation = {field: None for field in FIELDS}
        operation.update(op="update_task", target="task:20", duration_days=2)
        candidate = apply_operations(snapshot, {"schema_version": "1", "snapshot_revision": snapshot.revision,
            "message": "Example", "needs_clarification": False, "operations": [operation]})
        diff = compare_plans(plan, candidate)
        if not diff.changes or plan.tasks[1].duration == candidate.tasks[1].duration:
            raise RuntimeError("Local AI proposal preview failed")
        import httpx2
        response = {"schema_version": "1", "snapshot_revision": snapshot.revision,
                    "message": "Example", "needs_clarification": False, "operations": [operation]}
        def fake_server(request):
            return httpx2.Response(200, json={"id": "smoke", "object": "chat.completion", "created": 1,
                "model": "example", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(response)}}]})
        def fake_client(**kwargs):
            original = kwargs["http_client"]
            hooks = original.event_hooks
            original.close()
            kwargs["http_client"] = httpx2.Client(transport=httpx2.MockTransport(fake_server), event_hooks=hooks, follow_redirects=False)
            return OpenAI(**kwargs)
        offline = request_proposal(normalize_config("https://example.openai.azure.com", "example"),
                                   "offline-smoke-test", snapshot, "Change the child", client_factory=fake_client)
        if offline.candidate is None or offline.candidate.tasks[1].duration != 2:
            raise RuntimeError("Offline AI SDK request failed")
        html_path = directory / "smoke.html"
        export_html(plan, html_path)
        html = html_path.read_text(encoding="utf-8")
        if "<svg" not in html:
            raise RuntimeError("HTML report did not include a Gantt chart")
        if "08:00" in html:
            raise RuntimeError("HTML report included times by default")
        export_html(plan, html_path, show_time=True)
        if "08:00" not in html_path.read_text(encoding="utf-8"):
            raise RuntimeError("HTML report did not include requested times")
        for paper in ("A4", "A3"):
            pdf_path = directory / f"smoke-{paper}.pdf"
            export_pdf(plan, pdf_path, paper)
            if not pdf_path.read_bytes().startswith(b"%PDF-"):
                raise RuntimeError(f"{paper} PDF report could not be generated")
        # A template is deliberately never bundled. This optional local check
        # writes a disposable output and leaves the supplied template unchanged.
        if template := os.environ.get("ONEPYPROJECT_SMOKE_MPP_TEMPLATE"):
            template = Path(template)
            before = hashlib.sha256(template.read_bytes()).digest()
            mpp_path = directory / "smoke.mpp"
            export_mpp(plan, template, mpp_path)
            imported, _ = import_mpp(mpp_path)
            if [task.name for task in imported.tasks] != ["Parent", "Child", "After"]:
                raise RuntimeError("MPP export/import did not preserve test tasks")
            if hashlib.sha256(template.read_bytes()).digest() != before:
                raise RuntimeError("MPP template changed during smoke test")
        preferences = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
        window = MainWindow(preferences)
        if window.windowIcon().pixmap(QSize(128, 128)).isNull():
            raise RuntimeError("Main window application icon failed to render")
        window.plan = plan
        window.show()
        window.refresh()
        if window.tree.columnCount() != 10 or window.items[20].text(9) != "50.0%":
            raise RuntimeError("Task completion column did not render")
        app.processEvents()
        window.tree.setCurrentItem(window.items[20], 1)
        context = window.task_context_menu()
        actions = {action.text(): action for action in context.actions()}
        if not actions["Copy"].isEnabled() or actions["Link"].isEnabled():
            raise RuntimeError("Task context menu state is incorrect")
        context.deleteLater()
        window.open_ai()
        if window._ai_dialog.send.icon().pixmap(QSize(20, 20)).isNull():
            raise RuntimeError("AI composer icon failed to render")
        if "Example" not in window._ai_dialog.prompt._resource_names():
            raise RuntimeError("AI resource mention suggestions are unavailable")
        window.show_ai_preview(plan, candidate)
        app.processEvents()
        if not window.ai_preview.rows or window.gantt.grab().isNull():
            raise RuntimeError("AI preview interface did not render")
        window.clear_ai_preview()
        window._ai_dialog.close()
        if window.grab().isNull() or len(window.items) != 3:
            raise RuntimeError("Main window did not render the test plan")
        if __version__ not in window.windowTitle():
            raise RuntimeError("Application version is missing from the window title")
        dialog = ExportDialog(window, initial_format="pdf", initial_path=directory / "chosen.pdf")
        if dialog.show_time.isChecked() or dialog.paper.currentData() != "A4":
            raise RuntimeError("Export dialog defaults are incorrect")
        dialog.show()
        app.processEvents()
        dialog.accept()
        expected_export = directory / "chosen.pdf"
        actual_export = dialog.output_path
        if actual_export is None or actual_export.resolve() != expected_export.resolve():
            raise RuntimeError(
                "Export dialog did not accept the selected file: "
                f"result={dialog.result()}, output={actual_export!r}, "
                f"expected={expected_export!r}, directory={dialog.directory().absolutePath()!r}, "
                f"filename={dialog.filename_edit.text()!r}"
            )
        settings = SettingsDialog(plan, window)
        if settings.title.minimumWidth() < 400 or display_time(datetime(2026, 11, 11, 8), "short") != "11 Nov 08:00":
            raise RuntimeError("Settings or short date display is incorrect")
        if settings.tabs.count() != 2 or settings.density_selector.findData("dense") < 0:
            raise RuntimeError("Settings tabs or density control are missing")
        window.add_inline_task("Inserted", before_uid=20)
        if window.items[20].text(0) != "3" or window.items[30].text(5) != "3":
            raise RuntimeError("Displayed task numbers or predecessors did not update")
        window.tree.setCurrentItem(window.items[20], 8)
        window.tree.editItem(window.items[20], 8)
        app.processEvents()
        mode = window.tree.findChild(QComboBox)
        if mode is None or mode.width() < mode.sizeHint().width() or mode.height() < mode.sizeHint().height():
            raise RuntimeError("Mode editor did not fit its contents")
        mode.setCurrentIndex(mode.findData("work"))
        mode.activated.emit(mode.currentIndex())
        app.processEvents()
        app.processEvents()
        if next(task for task in window.plan.tasks if task.uid == 20).mode != "work":
            raise RuntimeError("Mode selection did not update the task")
        # The offscreen clipboard is private to this process; do not overwrite
        # the user's real clipboard during a native packaged smoke test.
        if app.platformName() == "offscreen":
            window.tree.setCurrentItem(window.items[30], 1)
            window.copy_rows()
            window.paste_rows()
            if window.plan.tasks[-1].name != "After" or window.plan.tasks[-1].predecessors != [20]:
                raise RuntimeError("Row copy and paste failed")
            window.undo()
            release_rows()
        window.tree.setColumnWidth(1, 315)
        def choose_density():
            dialog = QApplication.activeModalWidget()
            dialog.density_selector.setCurrentIndex(dialog.density_selector.findData("dense"))
            dialog.accept()
        QTimer.singleShot(0, choose_density)
        window.settings()
        window.dirty = False
        window.close()
        reopened = MainWindow(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat))
        if reopened.tree.columnWidth(1) != 315 or reopened.density != "dense":
            raise RuntimeError("Column widths or row density did not persist")
        reopened.close()
