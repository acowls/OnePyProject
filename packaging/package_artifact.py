"""Prepare a private CI artifact and test the extracted executable."""
import argparse
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent
VERSION = runpy.run_path(str(ROOT / "onepyproject" / "__init__.py"))["__version__"]
TARGETS = ("macos-arm64", "macos-x64", "windows-x64")
PRIVATE_EXTENSIONS = {".mpp", ".mpt", ".pxplan"}


def check_tree(directory):
    """Reject personal plans, broken/escaping links, and known unused Qt modules."""
    root = directory.resolve()
    names = set()
    for path in directory.rglob("*"):
        name = path.name.lower()
        names.add(name)
        if path.suffix.lower() in PRIVATE_EXTENSIONS:
            raise RuntimeError(f"Private project or template in artifact: {path}")
        if "qtwebengine" in name or name.startswith(("qt6pdf", "qtpdf")):
            raise RuntimeError(f"Unexpected Qt module in artifact: {path}")
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to(root):
                raise RuntimeError(f"Link escapes artifact: {path} -> {target}")
    if not any("qsvgicon" in name for name in names):
        raise RuntimeError("Qt SVG icon engine is missing")
    if not any(name in ("qsvg.dll", "libqsvg.dylib") for name in names):
        raise RuntimeError("Qt SVG image plugin is missing")
    if not any("qcocoa" in name or "qwindows.dll" in name for name in names):
        raise RuntimeError("Native Qt platform plugin is missing")


def copy_distribution_license(name, destination):
    distribution = metadata.distribution(name)
    candidates = [entry for entry in distribution.files or ()
                  if ".dist-info/" in str(entry).replace("\\", "/")
                  and entry.name.lower().startswith(("license", "copying"))]
    if len(candidates) != 1:
        raise RuntimeError(f"Could not identify the {name} licence: {candidates}")
    shutil.copyfile(distribution.locate_file(candidates[0]), destination)


def smoke_test(executable):
    with tempfile.TemporaryDirectory(prefix="onepyproject-check-") as temporary:
        report = Path(temporary) / "smoke.txt"
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run(
            [str(executable), "--packaging-smoke-test", str(report)],
            cwd=temporary, env=environment, capture_output=True, timeout=120,
        )
        details = report.read_text(encoding="utf-8") if report.exists() else "No report produced."
        if result.returncode or details.strip() != f"Packaging smoke test passed: {VERSION}.":
            raise RuntimeError(
                f"Frozen smoke test failed ({result.returncode}): {details}\n"
                f"stdout: {result.stdout.decode(errors='replace')}\n"
                f"stderr: {result.stderr.decode(errors='replace')}"
            )
        print(details.strip())


def make_artifact(target, dist, output):
    expected_system = "Darwin" if target.startswith("macos-") else "Windows"
    expected_machine = "arm64" if target.endswith("arm64") else "x64"
    machine = platform.machine().lower()
    actual_machine = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("x86_64", "amd64") else machine
    if platform.system() != expected_system or actual_machine != expected_machine:
        raise RuntimeError(f"Target {target} does not match {platform.system()} {machine}")

    name = f"OnePyProject-{VERSION}-{target}"
    output.mkdir(parents=True, exist_ok=True)
    archive = (output / f"{name}.zip").resolve()
    app_name = "OnePyProject.app" if expected_system == "Darwin" else "OnePyProject"
    with tempfile.TemporaryDirectory(prefix="onepyproject-package-") as temporary:
        temporary = Path(temporary)
        stage = temporary / name
        stage.mkdir()
        shutil.copytree(dist / app_name, stage / app_name, symlinks=True)
        shutil.copyfile(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md", stage / "THIRD_PARTY_NOTICES.md")
        shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
        licences = stage / "licenses"
        licences.mkdir()
        for dependency in ("pymppwriter", "olefile", "PyInstaller"):
            copy_distribution_license(dependency, licences / f"{dependency.lower()}.txt")
        shutil.copyfile(ROOT / "onepyproject" / "assets" / "flat_color_icons" / "LICENSE.txt", licences / "icons8.txt")
        manifest = {
            "target": target,
            "version": VERSION,
            "python": sys.version,
            "dependencies": {name: metadata.version(name) for name in
                             ("PySide6-Essentials", "shiboken6", "pymppwriter", "olefile", "PyInstaller")},
            "git_commit": os.environ.get("GITHUB_SHA", "unknown"),
            "warning": "Private test build; distribution compliance and signing are not validated.",
        }
        (stage / "BUILD_INFO.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        check_tree(stage)
        if expected_system == "Darwin":
            # GitHub's artifact ZIP does not preserve app symlinks/executable bits.
            subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(stage), str(archive)], check=True)
        else:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output_zip:
                for path in stage.rglob("*"):
                    if path.is_file():
                        output_zip.write(path, path.relative_to(temporary))

        extracted = temporary / "extracted"
        extracted.mkdir()
        if expected_system == "Darwin":
            subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        else:
            with zipfile.ZipFile(archive) as input_zip:
                input_zip.extractall(extracted)
        extracted_stage = extracted / name
        check_tree(extracted_stage)
        executable = (extracted_stage / app_name / "Contents" / "MacOS" / "OnePyProject"
                      if expected_system == "Darwin" else extracted_stage / app_name / "OnePyProject.exe")
        smoke_test(executable)
    print(f"Validated {archive}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    make_artifact(args.target, args.dist, args.output)
