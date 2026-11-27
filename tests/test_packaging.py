import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from onepyproject import __version__


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_artifact", ROOT / "packaging" / "package_artifact.py")
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)
source_spec = importlib.util.spec_from_file_location("fetch_sources", ROOT / "packaging" / "fetch_sources.py")
source_fetcher = importlib.util.module_from_spec(source_spec)
source_spec.loader.exec_module(source_fetcher)


def make_fake_bundle(path):
    path.mkdir()
    for name in ("qsvgicon.dll", "qsvg.dll", "qwindows.dll"):
        (path / name).touch()
    return path


def test_packaging_targets_exclude_macos_intel():
    assert packaging.TARGETS == ("macos-arm64", "windows-x64")


@pytest.mark.parametrize("filename", ["template.MPP", "other.Mpt", "personal.PxPlan"])
def test_package_rejects_personal_projects(tmp_path, filename):
    bundle = make_fake_bundle(tmp_path / "bundle")
    (bundle / filename).touch()
    with pytest.raises(RuntimeError, match="Private project or template"):
        packaging.check_tree(bundle)


@pytest.mark.parametrize("filename", ["Qt6Pdf.dll", "QtWebEngineCore.framework"])
def test_package_rejects_unused_qt_modules(tmp_path, filename):
    bundle = make_fake_bundle(tmp_path / "bundle")
    (bundle / filename).touch()
    with pytest.raises(RuntimeError, match="Unexpected Qt module"):
        packaging.check_tree(bundle)


def test_package_requires_svg_image_plugin(tmp_path):
    bundle = make_fake_bundle(tmp_path / "bundle")
    (bundle / "qsvg.dll").unlink()
    with pytest.raises(RuntimeError, match="SVG image plugin"):
        packaging.check_tree(bundle)


def test_package_rejects_a_different_frozen_version(monkeypatch):
    def run_other_version(args, **kwargs):
        Path(args[-1]).write_text("Packaging smoke test passed: 0.0.0.\n", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(packaging.subprocess, "run", run_other_version)
    with pytest.raises(RuntimeError, match="Frozen smoke test failed"):
        packaging.smoke_test(Path("app"))


def test_bundle_python_licenses_are_copied_from_frozen_metadata(tmp_path):
    bundle = tmp_path / "bundle"
    info = bundle / "sample_package-1.2.3.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample-package\nVersion: 1.2.3\nLicense-Expression: MIT\n",
        encoding="utf-8",
    )
    (info / "licenses" / "LICENSE.txt").write_text("MIT text\n", encoding="utf-8")
    (info / "licenses" / "NOTICE").write_text("Attribution\n", encoding="utf-8")
    license_root = tmp_path / "stage" / "licenses"

    packages = packaging.collect_bundled_python_licenses(bundle, license_root)

    assert packages == [{
        "name": "sample-package",
        "version": "1.2.3",
        "license": "MIT",
        "metadata": "sample_package-1.2.3.dist-info/METADATA",
        "license_files": [
            "licenses/python-packages/sample-package-1.2.3/licenses/LICENSE.txt",
            "licenses/python-packages/sample-package-1.2.3/licenses/NOTICE",
        ],
    }]
    assert (tmp_path / "stage" / packages[0]["license_files"][0]).read_text(encoding="utf-8") == "MIT text\n"


def test_bundle_python_license_inventory_rejects_missing_text(tmp_path):
    info = tmp_path / "bundle" / "sample-1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample\nVersion: 1\nLicense-Expression: MIT\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="No license/notice text"):
        packaging.collect_bundled_python_licenses(tmp_path / "bundle", tmp_path / "licenses")


def test_bundle_inventory_includes_editable_project_egg_info(tmp_path):
    bundle = tmp_path / "bundle"
    info = bundle / "onepyproject.egg-info"
    info.mkdir(parents=True)
    (info / "PKG-INFO").write_text(
        "Metadata-Version: 2.1\nName: OnePyProject\nVersion: 0.2.1\nLicense: MIT\n",
        encoding="utf-8",
    )
    (bundle / "LICENSE").write_text("MIT app license\n", encoding="utf-8")
    license_root = tmp_path / "stage" / "licenses"

    packages = packaging.collect_bundled_python_licenses(bundle, license_root)

    assert packages[0]["name"] == "OnePyProject"
    assert packages[0]["version"] == "0.2.1"
    assert packages[0]["license"] == "MIT"
    assert (tmp_path / "stage" / packages[0]["license_files"][0]).read_text(encoding="utf-8") == "MIT app license\n"


def test_native_inventory_records_hashes_for_mach_o_files(tmp_path):
    bundle = tmp_path / "bundle"
    binary = bundle / "Contents" / "Frameworks" / "PySide6" / "Qt" / "lib" / "QtCore.dylib"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfeQt library payload")

    inventory = packaging.bundled_native_inventory(bundle, "macos-arm64", "0.2.1")

    assert inventory["native_file_count"] == 1
    assert inventory["files"][0]["kind"] == "Mach-O"
    assert inventory["qt_runtime_files"] == ["Contents/Frameworks/PySide6/Qt/lib/QtCore.dylib"]
    assert re.fullmatch(r"[0-9a-f]{64}", inventory["files"][0]["sha256"])


def test_native_inventory_verification_detects_payload_changes(tmp_path):
    bundle = tmp_path / "bundle"
    binary = bundle / "library.dylib"
    bundle.mkdir()
    binary.write_bytes(b"\xcf\xfa\xed\xfeoriginal")
    inventory_path = tmp_path / "native.json"
    inventory_path.write_text(
        json.dumps(packaging.bundled_native_inventory(bundle, "macos-arm64", "0.2.1")),
        encoding="utf-8",
    )

    packaging.verify_native_inventory(bundle, inventory_path)
    binary.write_bytes(b"\xcf\xfa\xed\xfechanged")
    with pytest.raises(RuntimeError, match="does not match"):
        packaging.verify_native_inventory(bundle, inventory_path)


def test_corresponding_source_manifest_matches_qt_build_pins():
    manifest = json.loads((ROOT / "packaging" / "SOURCES.json").read_text(encoding="utf-8"))
    requirements = (ROOT / "packaging" / "requirements-build.txt").read_text(encoding="utf-8")
    source_notice = source_fetcher.render_sources_markdown(ROOT / "packaging" / "SOURCES.json")
    replacement_guide = (ROOT / "packaging" / "REPLACING_QT.md").read_text(encoding="utf-8")
    sources = {source["name"]: source for source in manifest["sources"]}
    assert sources["Qt"]["version"] == "6.12.0"
    assert sources["Qt for Python (PySide6 and Shiboken)"]["version"] == "6.12.0"
    for source in manifest["sources"]:
        assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        assert source["release_filename"]
        assert source["sha256"] in source_notice
        assert source["release_filename"] in source_notice
    assert "PySide6-Essentials==6.12.0" in requirements
    assert "shiboken6==6.12.0" in requirements
    assert "PySide6-Essentials and Shiboken **6.12.0**" in replacement_guide
    assert "Contents/Frameworks/PySide6/Qt/lib/Qt*.framework" in replacement_guide
    assert "_internal/PySide6/Qt6*.dll" in replacement_guide


def test_fetch_sources_verifies_and_saves_archive(monkeypatch, tmp_path):
    payload = b"source archive"
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps({"sources": [{
        "name": "Qt",
        "version": "6.12.0",
        "release_filename": "Qt-source.tar.xz",
        "url": "https://example.invalid/Qt-source.tar.xz",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }]}), encoding="utf-8")
    monkeypatch.setattr(source_fetcher, "urlopen", lambda *_args, **_kwargs: io.BytesIO(payload))

    files = source_fetcher.fetch_sources(tmp_path / "download", manifest)

    assert files == [tmp_path / "download" / "Qt-source.tar.xz"]
    assert files[0].read_bytes() == payload


def test_fetch_sources_rejects_bad_digest_without_leaving_partial_file(monkeypatch, tmp_path):
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps({"sources": [{
        "name": "Qt",
        "version": "6.12.0",
        "release_filename": "Qt-source.tar.xz",
        "url": "https://example.invalid/Qt-source.tar.xz",
        "sha256": "0" * 64,
    }]}), encoding="utf-8")
    monkeypatch.setattr(source_fetcher, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"bad source"))

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        source_fetcher.fetch_sources(tmp_path / "download", manifest)
    assert not (tmp_path / "download" / "Qt-source.tar.xz").exists()
    assert (tmp_path / "download" / "SOURCES.md").is_file()
    assert not list((tmp_path / "download").glob(".Qt-source.tar.xz.*"))


def test_notice_and_license_files_are_embedded_inside_macos_app(tmp_path):
    bundle = tmp_path / "OnePyProject.app"
    resources = bundle / "Contents" / "Resources"
    resources.mkdir(parents=True)
    stage = tmp_path / "release-folder"
    (stage / "licenses" / "python-packages" / "sample").mkdir(parents=True)
    for filename in ("THIRD_PARTY_NOTICES.md", "REPLACING_QT.md", "SOURCES.md", "SOURCES.json",
                     "BUNDLED_NATIVE_COMPONENTS.json", "BUILD_INFO.json"):
        (stage / filename).write_text(filename, encoding="utf-8")
    (stage / "licenses" / "python-packages" / "sample" / "LICENSE").write_text("license", encoding="utf-8")

    packaging.embed_notice_assets(bundle, stage, "Darwin")

    assert (resources / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8") == "THIRD_PARTY_NOTICES.md"
    assert (resources / "REPLACING_QT.md").is_file()
    assert not (resources / "BUNDLED_NATIVE_COMPONENTS.json").exists()
    assert (resources / "licenses" / "python-packages" / "sample" / "LICENSE").read_text(encoding="utf-8") == "license"


def test_source_packaging_smoke(tmp_path):
    report = tmp_path / "report.txt"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "frozen_entry.py"), "--packaging-smoke-test", str(report)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, report.read_text(encoding="utf-8") if report.exists() else result.stderr
    assert report.read_text(encoding="utf-8").strip() == f"Packaging smoke test passed: {__version__}."


def test_native_application_icons_are_valid():
    icon_dir = ROOT / "packaging" / "icons"
    windows_icon = (icon_dir / "OnePyProject.ico").read_bytes()
    macos_icon = (icon_dir / "OnePyProject.icns").read_bytes()
    assert windows_icon[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(windows_icon[4:6], "little") > 0
    assert macos_icon[:4] == b"icns"
    assert int.from_bytes(macos_icon[4:8], "big") == len(macos_icon)
