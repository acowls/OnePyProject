import json

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from onepyproject.app import MainWindow, SettingsDialog
from onepyproject.updates import (RELEASE_DOWNLOAD_PREFIX, RELEASE_PAGE_PREFIX, UpdateCheckError, fetch_latest_release,
                                  parse_latest_release, release_asset_name, supported_target)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def release_payload(tag="v0.3.0", target="macos-arm64", **overrides):
    version = tag[1:]
    asset = release_asset_name(version, target)
    payload = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"{RELEASE_PAGE_PREFIX}{tag}",
        "body": "Release notes",
        "assets": [{
            "name": asset,
            "browser_download_url": f"{RELEASE_DOWNLOAD_PREFIX}{tag}/{asset}",
            "size": 123,
        }],
    }
    payload.update(overrides)
    return payload


def test_supported_targets_are_limited_to_packaged_architectures():
    assert supported_target("darwin", "arm64") == "macos-arm64"
    assert supported_target("darwin", "x86_64") is None
    assert supported_target("win32", "AMD64") == "windows-x64"
    assert supported_target("linux", "x86_64") is None


def test_release_asset_name_is_stable_for_supported_targets():
    assert release_asset_name("0.3.0", "macos-arm64") == "OnePyProject-0.3.0-macos-arm64.dmg"
    assert release_asset_name("0.3.0", "windows-x64") == "OnePyProject-0.3.0-windows-x64.zip"
    with pytest.raises(ValueError, match="Unsupported release target"):
        release_asset_name("0.3.0", "macos-x64")


@pytest.mark.parametrize("target", ["macos-arm64", "windows-x64"])
def test_latest_release_returns_matching_platform_update(target):
    info = parse_latest_release(release_payload(target=target), "0.2.1", target)
    assert info.version == "0.3.0"
    extension = "dmg" if target == "macos-arm64" else "zip"
    assert info.asset_name == f"OnePyProject-0.3.0-{target}.{extension}"
    assert info.page_url == f"{RELEASE_PAGE_PREFIX}v0.3.0"
    assert info.release_notes == "Release notes"


def test_current_or_older_release_is_not_offered():
    assert parse_latest_release(release_payload(tag="v0.2.1"), "0.2.1", "macos-arm64") is None
    assert parse_latest_release(release_payload(tag="v0.2.0"), "0.2.1", "macos-arm64") is None


@pytest.mark.parametrize("flags", [{"draft": True}, {"prerelease": True}])
def test_draft_and_prerelease_are_never_offered(flags):
    assert parse_latest_release(release_payload(**flags), "0.2.1", "macos-arm64") is None


def test_release_requires_matching_asset_and_trusted_github_urls():
    with pytest.raises(UpdateCheckError, match="no windows-x64 package"):
        parse_latest_release(release_payload(), "0.2.1", "windows-x64")

    payload = release_payload()
    payload["assets"][0]["browser_download_url"] = "https://evil.example/update.zip"
    with pytest.raises(UpdateCheckError, match="not a trusted GitHub release URL"):
        parse_latest_release(payload, "0.2.1", "macos-arm64")

    payload = release_payload()
    payload["html_url"] = "https://github.com.evil.example/acowls/OnePyProject/releases/tag/v0.3.0"
    with pytest.raises(UpdateCheckError, match="release page URL"):
        parse_latest_release(payload, "0.2.1", "macos-arm64")


def test_only_stable_semver_tags_are_accepted():
    with pytest.raises(UpdateCheckError, match="stable vMAJOR.MINOR.PATCH"):
        parse_latest_release(release_payload(tag="v0.3.0-rc.1"), "0.2.1", "macos-arm64")


def test_fetch_latest_release_uses_fixed_github_api_and_request_headers(monkeypatch):
    payload = release_payload()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return json.dumps(payload).encode("utf-8")

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["accept"] = request.get_header("Accept")
        captured["user_agent"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("onepyproject.updates.urlopen", fake_urlopen)
    result = fetch_latest_release("0.2.1", "macos-arm64")
    assert result.version == "0.3.0"
    assert captured == {
        "url": "https://api.github.com/repos/acowls/OnePyProject/releases/latest",
        "accept": "application/vnd.github+json",
        "user_agent": "OnePyProject-update-check",
        "timeout": 10,
    }


def test_update_settings_persist_per_user_and_cancel_does_not_apply(qt_app, tmp_path, monkeypatch):
    # Kept as a Qt test without relying on the system's global QSettings location.
    path = str(tmp_path / "settings.ini")
    preferences = QSettings(path, QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    assert window.update_checks_enabled is True
    assert window.update_interval == "weekly"

    def accept_settings(dialog):
        dialog.update_checks_enabled.setChecked(False)
        dialog.update_interval.setCurrentIndex(dialog.update_interval.findData("daily"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SettingsDialog, "exec", accept_settings)
    window.settings()
    assert window.update_checks_enabled is False
    assert window.update_interval == "daily"
    reopened = MainWindow(QSettings(path, QSettings.Format.IniFormat))
    assert reopened.update_checks_enabled is False
    assert reopened.update_interval == "daily"

    def cancel_settings(dialog):
        dialog.update_checks_enabled.setChecked(True)
        dialog.update_interval.setCurrentIndex(dialog.update_interval.findData("monthly"))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(SettingsDialog, "exec", cancel_settings)
    reopened.settings()
    assert MainWindow(QSettings(path, QSettings.Format.IniFormat)).update_checks_enabled is False
    assert MainWindow(QSettings(path, QSettings.Format.IniFormat)).update_interval == "daily"
    window.close()
    reopened.close()
