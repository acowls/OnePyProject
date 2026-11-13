"""Offline Flat Color Icons rendered by Qt's scalable SVG icon engine."""
from pathlib import Path
import logging

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle


ASSET_DIR = Path(__file__).resolve().parent / "assets" / "flat_color_icons"
ICON_FILES = {
    "new": "file.svg",
    "open": "opened_folder.svg",
    "save": "download.svg",
    "save_as": "document.svg",
    "import": "import.svg",
    "export": "export.svg",
    "settings": "settings.svg",
    "task": "add_row.svg",
    "edit": "pencil.svg",
    "delete": "empty_trash.svg",
    "undo": "undo.svg",
    "redo": "redo.svg",
    "link": "link.svg",
    "unlink": "broken_link.svg",
    "indent": "right.svg",
    "outdent": "left.svg",
    "resources": "business_contact.svg",
    "assign": "collaboration.svg",
}


def toolbar_icon(name: str) -> QIcon:
    path = ASSET_DIR / ICON_FILES[name]
    # Constructing QIcon directly from the SVG keeps it scalable. Converting it
    # to a 20px QPixmap first would blur the result on high-DPI displays.
    icon = QIcon(str(path)) if path.is_file() else QIcon()
    if not icon.isNull() and not icon.pixmap(20, 20).isNull():
        return icon
    logging.getLogger(__name__).warning("Unable to render toolbar icon %s", path)
    return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
