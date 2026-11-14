import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from onepyproject import __version__


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_artifact", ROOT / "packaging" / "package_artifact.py")
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


def make_fake_bundle(path):
    path.mkdir()
    for name in ("qsvgicon.dll", "qsvg.dll", "qwindows.dll"):
        (path / name).touch()
    return path


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


def test_source_packaging_smoke(tmp_path):
    report = tmp_path / "report.txt"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "frozen_entry.py"), "--packaging-smoke-test", str(report)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, report.read_text(encoding="utf-8") if report.exists() else result.stderr
    assert report.read_text(encoding="utf-8").strip() == f"Packaging smoke test passed: {__version__}."
