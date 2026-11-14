"""Exercise packaged resources without using a user's settings or project files."""
import hashlib
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QSettings, QSize
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QComboBox

import pymppwriter
from . import __version__
from .app import ExportDialog, MainWindow, SettingsDialog, display_time
from .icons import ASSET_DIR, ICON_FILES
from .model import Plan, Resource, ResourceAssignment, Task, load_plan, save_plan
from .mpp import export_mpp, import_mpp
from .report import export_html
from .report_pdf import export_pdf


def run():
    app = QApplication([])
    app.setApplicationName("OnePyProject packaging smoke test")
    if pymppwriter.__version__ != "0.4.3":
        raise RuntimeError(f"Unexpected pymppwriter version: {pymppwriter.__version__}")

    # Bypass toolbar_icon's fallback so missing SVG plugins cannot pass silently.
    for filename in ICON_FILES.values():
        image = QIcon(str(ASSET_DIR / filename)).pixmap(QSize(20, 20), 2.0).toImage()
        if image.isNull() or not any(
            image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height())
        ):
            raise RuntimeError(f"SVG icon failed to render: {filename}")
    for filename in ("tree_open.svg", "tree_closed.svg"):
        image = QImage(str(ASSET_DIR.parent / "ui" / filename))
        if image.isNull():
            raise RuntimeError(f"SVG image failed to load: {filename}")

    with TemporaryDirectory(prefix="onepyproject-smoke-") as directory:
        directory = Path(directory)
        plan = Plan(start="2026-11-16", resources=[Resource(1, "Example")],
                    tasks=[Task(10, "Parent"), Task(20, "Child", parent=10, assignments=[ResourceAssignment(1)]),
                           Task(30, "After", predecessors=[20])])
        path = directory / "smoke.pxplan"
        save_plan(plan, path)
        plan = load_plan(path)
        if [task.name for task in plan.tasks] != ["Parent", "Child", "After"]:
            raise RuntimeError("Plan did not survive a save/load round trip")
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
        window.plan = plan
        window.show()
        window.refresh()
        app.processEvents()
        if window.grab().isNull() or len(window.items) != 3:
            raise RuntimeError("Main window did not render the test plan")
        if __version__ not in window.windowTitle():
            raise RuntimeError("Application version is missing from the window title")
        dialog = ExportDialog(window, initial_format="pdf", initial_path=directory / "chosen.pdf")
        if dialog.show_time.isChecked() or dialog.paper.currentData() != "A4":
            raise RuntimeError("Export dialog defaults are incorrect")
        dialog.accept()
        if dialog.output_path != directory / "chosen.pdf":
            raise RuntimeError("Export dialog did not accept the selected file")
        settings = SettingsDialog(plan, window)
        if settings.title.minimumWidth() < 400 or display_time(datetime(2026, 11, 11, 8), "short") != "11 Nov 08:00":
            raise RuntimeError("Settings or short date display is incorrect")
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
        window.tree.setColumnWidth(1, 315)
        window.density_selector.setCurrentIndex(window.density_selector.findData("dense"))
        window.dirty = False
        window.close()
        reopened = MainWindow(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat))
        if reopened.tree.columnWidth(1) != 315 or reopened.density != "dense":
            raise RuntimeError("Column widths or row density did not persist")
        reopened.close()
