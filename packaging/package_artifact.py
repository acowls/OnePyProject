"""Build, package, and smoke-test a desktop artifact."""
import argparse
from email.parser import Parser
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import platform
import runpy
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent
VERSION = runpy.run_path(str(ROOT / "onepyproject" / "__init__.py"))["__version__"]
TARGETS = ("macos-arm64", "windows-x64")
PRIVATE_EXTENSIONS = {".mpp", ".mpt", ".pxplan"}
_source_helpers_spec = importlib.util.spec_from_file_location(
    "_onepyproject_fetch_sources", ROOT / "packaging" / "fetch_sources.py"
)
_source_helpers = importlib.util.module_from_spec(_source_helpers_spec)
_source_helpers_spec.loader.exec_module(_source_helpers)
LICENSE_PREFIXES = ("license", "licence", "copying", "notice", "copyright")
MACH_O_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


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


def _safe_component(value):
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", value).strip("._") or "unknown"


def _license_files(directory):
    return sorted(
        path for path in directory.rglob("*")
        if path.is_file() and path.name.lower().startswith(LICENSE_PREFIXES)
    )


def _license_declaration(message):
    expression = message.get("License-Expression")
    if expression:
        return expression.strip()
    license_text = message.get("License")
    if license_text and license_text.strip().lower() not in {"unknown", "n/a", "none"}:
        return license_text.strip()
    classifiers = [
        value.partition("::")[2].strip()
        for value in message.get_all("Classifier", [])
        if value.startswith("License ::")
    ]
    return "; ".join(classifiers)


def collect_bundled_python_licenses(bundle, license_root):
    """Copy license texts from the distributions actually present in the frozen app."""
    found = {}
    metadata_files = [*bundle.rglob("*.dist-info/METADATA"), *bundle.rglob("*.egg-info/PKG-INFO")]
    for metadata_file in sorted(metadata_files):
        message = Parser().parsestr(metadata_file.read_text(encoding="utf-8", errors="replace"))
        name = (message.get("Name") or "").strip()
        version = (message.get("Version") or "").strip()
        declaration = _license_declaration(message)
        if not name or not version or not declaration:
            raise RuntimeError(f"Incomplete package identity or license metadata: {metadata_file}")
        key = (name.casefold().replace("_", "-"), version)
        if key in found:
            continue

        texts = _license_files(metadata_file.parent)
        if not texts:
            normalized_name = name.casefold().replace("_", "-")
            if normalized_name == "onepyproject":
                own_license = bundle / "Contents" / "Resources" / "LICENSE"
                if not own_license.is_file():
                    own_license = bundle / "LICENSE"
                if not own_license.is_file():
                    raise RuntimeError("The app's MIT license is missing from the frozen bundle")
                texts = [own_license]
            elif normalized_name in {"pyside6-essentials", "shiboken6"}:
                texts = [license_root / license_name for license_name in
                         ("LGPL-3.0.txt", "GPL-2.0.txt", "GPL-3.0.txt")]
                if not all(path.is_file() for path in texts):
                    raise RuntimeError(f"Missing Qt/PySide license texts for {name} {version}")
            else:
                raise RuntimeError(f"No license/notice text shipped for bundled Python package {name} {version}")
        destination = license_root / "python-packages" / f"{_safe_component(name)}-{_safe_component(version)}"
        copied = []
        for source in texts:
            if source.parent == license_root:
                copied.append(source)
                continue
            try:
                relative = source.relative_to(metadata_file.parent)
            except ValueError:
                relative = Path(source.name)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target)
        found[key] = {
            "name": name,
            "version": version,
            "license": declaration,
            "metadata": metadata_file.relative_to(bundle).as_posix(),
            "license_files": [path.relative_to(license_root.parent).as_posix() for path in copied],
        }

    if not found:
        raise RuntimeError(f"No bundled Python distribution metadata found in {bundle}")
    return sorted(found.values(), key=lambda item: (item["name"].casefold(), item["version"]))


def copy_distribution_licenses(name, destination):
    """Copy all license and notice files for a build tool, not just the first match."""
    distribution = metadata.distribution(name)
    entries = list(distribution.files or ())
    metadata_entry = next(
        (entry for entry in entries if entry.name == "METADATA" and Path(str(entry)).parts[0].endswith(".dist-info")),
        None,
    )
    if metadata_entry is None:
        raise RuntimeError(f"Could not identify {name}'s own distribution metadata")
    distribution_info = Path(str(metadata_entry)).parts[0]
    candidates = [entry for entry in distribution.files or ()
                  if Path(str(entry)).parts[0] == distribution_info
                  and entry.name.lower().startswith(LICENSE_PREFIXES)]
    if not candidates:
        raise RuntimeError(f"Could not find license/notice text for build dependency {name}")
    target_dir = destination / f"{_safe_component(distribution.metadata['Name'])}-{distribution.version}"
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for entry in candidates:
        source = distribution.locate_file(entry)
        target = target_dir / entry.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            target = target_dir / f"{entry.parent.name}-{entry.name}"
        shutil.copyfile(source, target)
        copied.append(target)
    return copied


def copy_python_license(destination):
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    candidates = [
        Path(sys.base_prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    ]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError(f"Could not find the Python {version} license text in {sys.base_prefix}")
    target = destination / f"Python-{version}" / "LICENSE.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def _native_kind(path):
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic == b"\x7fELF":
        return "ELF"
    if magic.startswith(b"MZ"):
        return "PE"
    if magic in MACH_O_MAGICS:
        return "Mach-O"
    return None


def bundled_native_inventory(bundle, target, version):
    files = []
    qt_files = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        kind = _native_kind(path)
        if kind is None:
            continue
        relative = path.relative_to(bundle).as_posix()
        item = {
            "path": relative,
            "kind": kind,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        files.append(item)
        if "/PySide6/Qt/" in f"/{relative}":
            qt_files.append(relative)
    if not files:
        raise RuntimeError(f"No native binaries found in packaged application: {bundle}")
    qt_plugin_categories = sorted({
        Path(path).parts[Path(path).parts.index("plugins") + 1]
        for path in qt_files
        if "plugins" in Path(path).parts and len(Path(path).parts) > Path(path).parts.index("plugins") + 1
    })
    return {
        "schema_version": 1,
        "application": "OnePyProject",
        "version": version,
        "target": target,
        "qt_runtime_version": metadata.version("PySide6-Essentials"),
        "pyside6_essentials_version": metadata.version("PySide6-Essentials"),
        "shiboken6_version": metadata.version("shiboken6"),
        "native_file_count": len(files),
        "qt_plugin_categories": qt_plugin_categories,
        "qt_runtime_files": qt_files,
        "files": files,
    }


def verify_native_inventory(bundle, inventory_path):
    expected = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    actual = bundled_native_inventory(bundle, expected["target"], expected["version"])
    if actual != expected:
        raise RuntimeError(f"Native-file inventory does not match the extracted app: {bundle}")


def write_third_party_notices(destination, packages, build_tools, native_inventory):
    template = (ROOT / "packaging" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").rstrip()
    lines = [template, "", "## Python distributions found in this build", "",
             "The following inventory is generated from the `.dist-info/METADATA` and license/notice files "
             "inside the frozen application. It records upstream package metadata; it is not a legal review.", "",
             "| Distribution | Version | Declared license | Included license/notice files |",
             "|---|---:|---|---|"]
    for package in packages:
        licenses = ", ".join(f"[`{Path(path).name}`]({path})" for path in package["license_files"])
        lines.append(f"| {package['name']} | {package['version']} | {package['license']} | {licenses} |")
    lines.extend(["", "## Build tools and runtime licenses", "",
                  f"The Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} license is in "
                  f"`licenses/Python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}/LICENSE.txt`.",
                  "Build-tool license/notice files:"])
    for tool in build_tools:
        paths = ", ".join(f"`{path}`" for path in tool["license_files"])
        lines.append(f"- {tool['name']} {tool['version']}: {paths}")
    lines.extend([
                  f"This `{native_inventory['target']}` build bundles Qt/PySide {native_inventory['qt_runtime_version']} "
                  f"with {native_inventory['native_file_count']} native files and plugin categories "
                  f"{', '.join(native_inventory['qt_plugin_categories']) or 'none detected'}.",
                  "Qt and PySide6 licensing/source references and exact source archive hashes are in `SOURCES.md`.",
                  "The separate `BUNDLED_NATIVE_COMPONENTS.json` lists native files and SHA-256 hashes discovered in this build.", ""])
    destination.write_text("\n".join(lines), encoding="utf-8")


def write_legacy_license_aliases(license_root, stage, packages, build_tools):
    """Keep the existing flat notice paths while also shipping the full inventory tree."""
    package_by_name = {package["name"].casefold().replace("_", "-"): package for package in packages}
    for name in ("pymppwriter", "olefile", "openai", "keyring", "google-auth", "requests"):
        package = package_by_name[name]
        candidates = [path for path in package["license_files"]
                      if Path(path).name.lower().startswith(("license", "copying"))]
        if not candidates:
            raise RuntimeError(f"No primary license file to copy for {name}")
        shutil.copyfile(stage / candidates[0], license_root / f"{name}.txt")

    pyinstaller = next((tool for tool in build_tools if tool["name"].casefold() == "pyinstaller"), None)
    if pyinstaller is None or not pyinstaller["license_files"]:
        raise RuntimeError("PyInstaller's bootloader license is missing")
    shutil.copyfile(stage / pyinstaller["license_files"][0], license_root / "pyinstaller.txt")


def embed_notice_assets(bundle, stage, system):
    """Keep notices and license texts with the app if users move it out of the ZIP folder."""
    destination = bundle / "Contents" / "Resources" if system == "Darwin" else bundle
    destination.mkdir(parents=True, exist_ok=True)
    filenames = ["THIRD_PARTY_NOTICES.md", "REPLACING_QT.md", "SOURCES.md", "SOURCES.json", "BUILD_INFO.json"]
    # The final native inventory contains the app executable's post-signing hash. Keep it
    # beside a macOS .app rather than sealing a pre-signing copy inside the bundle.
    if system != "Darwin":
        filenames.append("BUNDLED_NATIVE_COMPONENTS.json")
    for filename in filenames:
        shutil.copyfile(stage / filename, destination / filename)
    shutil.copytree(stage / "licenses", destination / "licenses", dirs_exist_ok=True)


def verify_macos_bundle_signature(bundle):
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(bundle)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"macOS app bundle signature verification failed: {bundle}\n{result.stdout}\n{result.stderr}"
        )


def ad_hoc_sign_macos_bundle(bundle):
    """Reseal the bundle after adding notices; this is not a Developer ID signature."""
    result = subprocess.run(
        ["codesign", "--force", "--sign", "-", str(bundle)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"Could not ad-hoc sign macOS app bundle: {bundle}\n{result.stderr}")
    verify_macos_bundle_signature(bundle)


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
        shutil.copyfile(ROOT / "packaging" / "REPLACING_QT.md", stage / "REPLACING_QT.md")
        shutil.copyfile(ROOT / "packaging" / "SOURCES.json", stage / "SOURCES.json")
        (stage / "SOURCES.md").write_text(
            _source_helpers.render_sources_markdown(ROOT / "packaging" / "SOURCES.json"), encoding="utf-8"
        )
        shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
        licences = stage / "licenses"
        licences.mkdir()
        shutil.copyfile(ROOT / "packaging" / "licenses" / "LGPL-3.0.txt", licences / "LGPL-3.0.txt")
        shutil.copyfile(ROOT / "packaging" / "licenses" / "GPL-2.0.txt", licences / "GPL-2.0.txt")
        shutil.copyfile(ROOT / "packaging" / "licenses" / "GPL-3.0.txt", licences / "GPL-3.0.txt")
        shutil.copyfile(ROOT / "onepyproject" / "assets" / "flat_color_icons" / "LICENSE.txt", licences / "icons8.txt")
        bundle = stage / app_name
        packages = collect_bundled_python_licenses(bundle, licences)
        copy_python_license(licences)
        build_tool_licenses = []
        for dependency in ("PyInstaller", "pyinstaller-hooks-contrib", "setuptools"):
            paths = copy_distribution_licenses(dependency, licences / "build-tools")
            build_tool_licenses.append({
                "name": metadata.distribution(dependency).metadata["Name"],
                "version": metadata.version(dependency),
                "license_files": [path.relative_to(stage).as_posix() for path in paths],
            })
        write_legacy_license_aliases(licences, stage, packages, build_tool_licenses)
        native_inventory = bundled_native_inventory(bundle, target, VERSION)
        write_third_party_notices(stage / "THIRD_PARTY_NOTICES.md", packages, build_tool_licenses, native_inventory)
        (stage / "BUNDLED_NATIVE_COMPONENTS.json").write_text(
            json.dumps(native_inventory, indent=2) + "\n", encoding="utf-8"
        )
        manifest = {
            "target": target,
            "version": VERSION,
            "python": sys.version,
            "dependencies": {name: metadata.version(name) for name in
                             ("PySide6-Essentials", "shiboken6", "PyInstaller", "pyinstaller-hooks-contrib")},
            "bundled_python_distributions": packages,
            "native_file_count": native_inventory["native_file_count"],
            "pymppwriter_source": distribution_source("pymppwriter"),
            "git_commit": os.environ.get("GITHUB_SHA", "unknown"),
            "platform_signature": "ad-hoc; no Developer ID signature or notarization" if expected_system == "Darwin"
                                  else "no Authenticode signature",
            "warning": "No publisher-identity signature or notarization; inventory and any SBOM are review inputs, not compliance certification.",
        }
        (stage / "BUILD_INFO.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        embed_notice_assets(bundle, stage, expected_system)
        if expected_system == "Darwin":
            # PyInstaller initially signs the bundle ad hoc. Re-seal its resources after adding notices.
            ad_hoc_sign_macos_bundle(bundle)
        # Ad-hoc signing may change the main Mach-O executable. Record hashes only after signing.
        native_inventory = bundled_native_inventory(bundle, target, VERSION)
        (stage / "BUNDLED_NATIVE_COMPONENTS.json").write_text(
            json.dumps(native_inventory, indent=2) + "\n", encoding="utf-8"
        )
        if expected_system == "Windows":
            shutil.copyfile(stage / "BUNDLED_NATIVE_COMPONENTS.json", bundle / "BUNDLED_NATIVE_COMPONENTS.json")
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
        if expected_system == "Darwin":
            verify_macos_bundle_signature(extracted_stage / app_name)
        verify_native_inventory(
            extracted_stage / app_name,
            extracted_stage / "BUNDLED_NATIVE_COMPONENTS.json",
        )
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
