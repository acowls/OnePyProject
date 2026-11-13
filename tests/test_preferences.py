from PySide6.QtCore import QSettings

from onepyproject.preferences import LEGACY_KEYS, MIGRATION_MARKER, migrate_legacy_preferences


def test_legacy_preferences_are_copied_once_without_overwriting(tmp_path):
    old_path = str(tmp_path / "old.ini")
    new_path = str(tmp_path / "new.ini")
    legacy = QSettings(old_path, QSettings.Format.IniFormat)
    current = QSettings(new_path, QSettings.Format.IniFormat)
    values = [False, True, "long", "dense", 18]
    for key, value in zip(LEGACY_KEYS, values):
        legacy.setValue(key, value)
    legacy.sync()
    original = (tmp_path / "old.ini").read_bytes()
    current.setValue("display/date_format", "short")
    assert migrate_legacy_preferences(current, legacy)
    assert current.value("keyboard/digit_edits_duration", type=bool) is False
    assert current.value("display/show_icon_labels", type=bool) is True
    assert current.value("display/date_format") == "short"
    assert current.value("display/density") == "dense"
    assert current.value("display/zoom_width", type=int) == 18
    assert current.value(MIGRATION_MARKER, type=bool) is True
    assert (tmp_path / "old.ini").read_bytes() == original
    current.remove("display/density")
    current.sync()
    assert not migrate_legacy_preferences(current, legacy)
    assert not current.contains("display/density")


def test_empty_legacy_preferences_do_not_write_a_migration_marker(tmp_path):
    legacy = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    current = QSettings(str(tmp_path / "new.ini"), QSettings.Format.IniFormat)
    assert not migrate_legacy_preferences(current, legacy)
    assert not current.contains(MIGRATION_MARKER)
