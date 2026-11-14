"""Exercise packaged resources without using a user's settings or project files."""
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QSettings, QSize
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication

import pymppwriter
from . import __version__
from .app import MainWindow
from .icons import ASSET_DIR, ICON_FILES
from .model import Plan, Task, load_plan, save_plan
from .mpp import export_mpp, import_mpp


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
        plan = Plan(start="2026-11-16", tasks=[Task(1, "Parent"), Task(2, "Child", parent=1)])
        path = directory / "smoke.pxplan"
        save_plan(plan, path)
        plan = load_plan(path)
        if [task.name for task in plan.tasks] != ["Parent", "Child"]:
            raise RuntimeError("Plan did not survive a save/load round trip")
        # A template is deliberately never bundled. This optional local check
        # writes a disposable output and leaves the supplied template unchanged.
        if template := os.environ.get("ONEPYPROJECT_SMOKE_MPP_TEMPLATE"):
            template = Path(template)
            before = hashlib.sha256(template.read_bytes()).digest()
            mpp_path = directory / "smoke.mpp"
            export_mpp(plan, template, mpp_path)
            imported, _ = import_mpp(mpp_path)
            if [task.name for task in imported.tasks] != ["Parent", "Child"]:
                raise RuntimeError("MPP export/import did not preserve test tasks")
            if hashlib.sha256(template.read_bytes()).digest() != before:
                raise RuntimeError("MPP template changed during smoke test")
        preferences = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
        window = MainWindow(preferences)
        window.plan = plan
        window.show()
        window.refresh()
        app.processEvents()
        if window.grab().isNull() or len(window.items) != 2:
            raise RuntimeError("Main window did not render the test plan")
        if __version__ not in window.windowTitle():
            raise RuntimeError("Application version is missing from the window title")
        window.close()
