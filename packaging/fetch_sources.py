"""Download and verify the Qt source archives associated with a desktop release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "packaging" / "SOURCES.json"


def render_sources_markdown(manifest=DEFAULT_MANIFEST):
    """Render the release source note from the machine-readable manifest."""
    sources = json.loads(Path(manifest).read_text(encoding="utf-8"))["sources"]
    lines = [
        "# Corresponding source archives",
        "",
        "The desktop builds use the Qt and Qt for Python community releases below. The release workflow downloads these source archives, verifies their SHA-256 hashes, and attaches them to the same GitHub Release as the app ZIPs. `SHA256SUMS.txt` covers the release assets.",
        "",
        "| Component | Version | Release asset | Upstream source | SHA-256 |",
        "|---|---:|---|---|---|",
    ]
    for source in sources:
        lines.append(
            f"| {source['name']} | {source['version']} | `{source['release_filename']}` "
            f"| [{source.get('filename', source['release_filename'])}]({source['url']}) | `{source['sha256']}` |"
        )
    lines.extend([
        "",
        "## Licensing and SBOM references",
        "",
        "- [Qt 6.12 licensing](https://doc.qt.io/qt-6.12/licensing.html)",
        "- [Third-party code used in Qt 6.12](https://doc.qt.io/qt-6.12/licenses-used-in-qt.html)",
        "- [Qt SBOM information](https://doc.qt.io/qt-6.12/sbom.html)",
        "- [Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html)",
        "- [Qt for Python LGPL guidance](https://doc.qt.io/qtforpython-6/overviews/qtdoc-lgpl.html)",
        "",
        "The source archives include upstream licensing and third-party notices. Compare the release's `BUNDLED_NATIVE_COMPONENTS.json` with the upstream licensing/SBOM information. These source links and hashes are a delivery plan, not a legal opinion that all source or installation-information duties have been met. Recheck the source versions and hashes whenever Qt/PySide versions change.",
        "",
        "Python package versions and license texts are listed in the bundle's generated `THIRD_PARTY_NOTICES.md`; the `pymppwriter` fork commit is recorded in `BUILD_INFO.json`.",
        "",
    ])
    return "\n".join(lines)


def fetch_sources(output, manifest=DEFAULT_MANIFEST):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    entries = json.loads(Path(manifest).read_text(encoding="utf-8"))["sources"]
    (output / "SOURCES.md").write_text(render_sources_markdown(manifest), encoding="utf-8")
    for source in entries:
        target = output / source["release_filename"]
        digest = hashlib.sha256()
        temporary_name = None
        try:
            with urlopen(source["url"], timeout=60) as response, tempfile.NamedTemporaryFile(
                dir=output, prefix=f".{target.name}.", delete=False
            ) as temporary:
                temporary_name = temporary.name
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    temporary.write(chunk)
            actual = digest.hexdigest()
            if actual != source["sha256"]:
                raise RuntimeError(
                    f"SHA-256 mismatch for {source['name']} {source['version']}: "
                    f"expected {source['sha256']}, got {actual}"
                )
            os.replace(temporary_name, target)
            temporary_name = None
            print(f"Verified {source['name']} {source['version']}: {target}")
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
    return [output / source["release_filename"] for source in entries]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Directory for the verified source archives")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args()
    fetch_sources(arguments.output, arguments.manifest)
