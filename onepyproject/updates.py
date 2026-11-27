"""Published-release update checks for packaged OnePyProject builds."""
from __future__ import annotations

from dataclasses import dataclass
import json
import platform
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


RELEASES_API_URL = "https://api.github.com/repos/acowls/OnePyProject/releases/latest"
RELEASE_PAGE_PREFIX = "https://github.com/acowls/OnePyProject/releases/tag/"
RELEASE_DOWNLOAD_PREFIX = "https://github.com/acowls/OnePyProject/releases/download/"
UPDATE_INTERVALS = {"daily": 1, "weekly": 7, "monthly": 30}
DEFAULT_UPDATE_INTERVAL = "weekly"
DEFAULT_AUTO_CHECK = True
NETWORK_TIMEOUT_SECONDS = 10
MAX_RELEASE_RESPONSE_BYTES = 1024 * 1024
_STABLE_VERSION = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class UpdateCheckError(RuntimeError):
    """The latest release could not be safely identified."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    page_url: str
    asset_name: str
    asset_url: str
    release_notes: str


def supported_target(system=None, machine=None):
    """Return the release target for this process, or None if it is unsupported."""
    system = sys.platform if system is None else system
    machine = platform.machine().lower() if machine is None else machine.lower()
    if system == "darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    if system == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    return None


def release_asset_name(version, target):
    """Return the stable release asset name shared by packaging and the updater."""
    if target not in ("macos-arm64", "windows-x64"):
        raise ValueError(f"Unsupported release target: {target}")
    extension = "dmg" if target == "macos-arm64" else "zip"
    return f"OnePyProject-{version}-{target}.{extension}"


def _stable_version(tag):
    if not isinstance(tag, str):
        raise UpdateCheckError("The release has no valid version tag.")
    match = _STABLE_VERSION.fullmatch(tag)
    if not match:
        raise UpdateCheckError("The release tag is not a stable vMAJOR.MINOR.PATCH version.")
    return tuple(int(part) for part in match.groups())


def _github_url(value, expected_url):
    if not isinstance(value, str) or value != expected_url:
        return False
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.query
            and not parsed.fragment)


def parse_latest_release(payload, current_version, target):
    """Validate GitHub release metadata and return an applicable update, if any."""
    if not isinstance(payload, dict):
        raise UpdateCheckError("GitHub returned invalid release metadata.")
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None

    tag = payload.get("tag_name")
    latest_version = _stable_version(tag)
    installed_version = _stable_version(f"v{current_version}")
    if latest_version <= installed_version:
        return None

    version = tag[1:]
    asset_name = release_asset_name(version, target)
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateCheckError("The release does not contain a valid asset list.")
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == asset_name), None)
    if asset is None:
        raise UpdateCheckError(f"The latest release has no {target} package.")

    asset_url = asset.get("browser_download_url")
    expected_asset_url = f"{RELEASE_DOWNLOAD_PREFIX}{tag}/{asset_name}"
    if not _github_url(asset_url, expected_asset_url):
        raise UpdateCheckError("The release package URL is not a trusted GitHub release URL.")

    page_url = payload.get("html_url")
    expected_page_url = f"{RELEASE_PAGE_PREFIX}{tag}"
    if not _github_url(page_url, expected_page_url):
        raise UpdateCheckError("The release page URL is not a trusted GitHub release URL.")

    notes = payload.get("body", "")
    if not isinstance(notes, str):
        notes = ""
    return UpdateInfo(version, tag, page_url, asset_name, asset_url, notes)


def fetch_latest_release(current_version, target=None):
    """Fetch and validate the latest published stable release from GitHub."""
    target = target or supported_target()
    if target not in ("macos-arm64", "windows-x64"):
        raise UpdateCheckError("This operating system or processor is not supported by the current release builds.")

    request = Request(
        RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "OnePyProject-update-check",
        },
    )
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError("Could not reach the OnePyProject releases service.") from exc
    if len(data) > MAX_RELEASE_RESPONSE_BYTES:
        raise UpdateCheckError("GitHub returned unexpectedly large release metadata.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("GitHub returned invalid release metadata.") from exc
    return parse_latest_release(payload, current_version, target)
