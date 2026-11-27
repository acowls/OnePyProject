"""Build, package, and smoke-test a desktop artifact."""
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from onepyproject.updates import release_asset_name

VERSION = runpy.run_path(str(ROOT / "onepyproject" / "__init__.py"))["__version__"]
TARGETS = ("macos-arm64", "windows-x64")
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


def distribution_source(name):
    """Say where a distribution was installed from, without exposing a local path."""
    text = metadata.distribution(name).read_text("direct_url.json")
    if not text:
        return "package index"
    info = json.loads(text)
    if info.get("vcs_info"):
        return f"{info['url']}@{info['vcs_info'].get('commit_id', 'unknown')}"
    return "local directory" if info["url"].startswith("file:") else info["url"]


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


def make_macos_dmg(stage, archive, temporary):
    """Create a drag-to-Applications disk image from a validated app bundle."""
    image_source = temporary / "dmg-source"
    shutil.copytree(stage, image_source, symlinks=True)
    (image_source / "Applications").symlink_to("/Applications", target_is_directory=True)
    (image_source / "INSTALL.txt").write_text(
        "Drag OnePyProject.app onto the Applications shortcut to install it.\n"
        "This build is unsigned; macOS security warnings are expected.\n",
        encoding="utf-8",
    )
    subprocess.run([
        "hdiutil", "create", "-volname", "OnePyProject", "-srcfolder", str(image_source),
        "-ov", "-format", "UDZO", str(archive),
    ], check=True)


def smoke_test_macos_dmg(archive, temporary, app_name):
    """Mount the generated image read-only and exercise the app from it."""
    mountpoint = temporary / "mounted-dmg"
    mountpoint.mkdir()
    subprocess.run([
        "hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(archive),
    ], check=True, capture_output=True)
    try:
        application = mountpoint / app_name
        if not (mountpoint / "Applications").is_symlink():
            raise RuntimeError("The macOS disk image is missing its Applications shortcut")
        if (mountpoint / "INSTALL.txt").read_text(encoding="utf-8").find("Drag OnePyProject.app") < 0:
            raise RuntimeError("The macOS disk image is missing its install instructions")
        check_tree(application)
        smoke_test(application / "Contents" / "MacOS" / "OnePyProject")
    finally:
        subprocess.run(["hdiutil", "detach", str(mountpoint), "-quiet"], check=True, capture_output=True)


def make_artifact(target, dist, output):
    expected_system = "Darwin" if target.startswith("macos-") else "Windows"
    expected_machine = "arm64" if target.endswith("arm64") else "x64"
    machine = platform.machine().lower()
    actual_machine = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("x86_64", "amd64") else machine
    if platform.system() != expected_system or actual_machine != expected_machine:
        raise RuntimeError(f"Target {target} does not match {platform.system()} {machine}")

    asset_name = release_asset_name(VERSION, target)
    name = Path(asset_name).stem
    output.mkdir(parents=True, exist_ok=True)
    archive = (output / asset_name).resolve()
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
        for dependency in ("pymppwriter", "olefile", "PyInstaller", "openai", "keyring", "google-auth", "requests"):
            copy_distribution_license(dependency, licences / f"{dependency.lower()}.txt")
        shutil.copyfile(ROOT / "onepyproject" / "assets" / "flat_color_icons" / "LICENSE.txt", licences / "icons8.txt")
        manifest = {
            "target": target,
            "version": VERSION,
            "python": sys.version,
            "dependencies": {name: metadata.version(name) for name in
                             ("PySide6-Essentials", "shiboken6", "pymppwriter", "olefile", "PyInstaller", "openai",
                              "keyring", "google-auth", "requests")},
            "pymppwriter_source": distribution_source("pymppwriter"),
            "git_commit": os.environ.get("GITHUB_SHA", "unknown"),
            "warning": "This artifact is unsigned; distribution compliance has not been validated.",
        }
        (stage / "BUILD_INFO.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        check_tree(stage)
        if expected_system == "Darwin":
            make_macos_dmg(stage, archive, temporary)
            smoke_test_macos_dmg(archive, temporary, app_name)
        else:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output_zip:
                for path in stage.rglob("*"):
                    if path.is_file():
                        output_zip.write(path, path.relative_to(temporary))
            extracted = temporary / "extracted"
            extracted.mkdir()
            with zipfile.ZipFile(archive) as input_zip:
                input_zip.extractall(extracted)
            extracted_stage = extracted / name
            check_tree(extracted_stage)
            smoke_test(extracted_stage / app_name / "OnePyProject.exe")
    print(f"Validated {archive}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    make_artifact(args.target, args.dist, args.output)
