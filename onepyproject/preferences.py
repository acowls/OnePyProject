"""Per-user preferences and non-destructive migration from ProjectX."""
from PySide6.QtCore import QSettings


LEGACY_KEYS = (
    "keyboard/digit_edits_duration",
    "display/show_icon_labels",
    "display/date_format",
    "display/density",
    "display/zoom_width",
)
MIGRATION_MARKER = "migration/projectx_preferences_copied"


def migrate_legacy_preferences(current: QSettings, legacy: QSettings) -> bool:
    """Copy known old keys once, never overwriting new values or changing old ones."""
    if current.value(MIGRATION_MARKER, False, type=bool):
        return False
    available = [key for key in LEGACY_KEYS if legacy.contains(key)]
    if not available:
        return False
    for key in available:
        if not current.contains(key):
            current.setValue(key, legacy.value(key))
    current.setValue(MIGRATION_MARKER, True)
    current.sync()
    return True


def application_preferences() -> QSettings:
    current = QSettings("OnePyProject", "OnePyProject")
    migrate_legacy_preferences(current, QSettings("ProjectX", "ProjectX"))
    return current
