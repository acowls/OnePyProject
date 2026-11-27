# Replacing Qt libraries in OnePyProject

This guide is practical guidance to meet the Qt LGPL requirement; replacing the QT libraries with one's own libraries. OnePyProject source is available at `https://github.com/acowls/OnePyProject` allowing full rebuild with ones own QT packages independent of the PyInstaller packaging.

## Source archives

The desktop build uses the Qt 6.12.0 and Qt for Python 6.12.0 community releases. The PySide6-Essentials and Shiboken wheel metadata advertises LGPL-3.0-only, GPL-2.0-only, or GPL-3.0-only licensing. The release workflow downloads the corresponding upstream source archives, checks their SHA-256 digests, and attaches them to the same GitHub Release as the app bundles. `SHA256SUMS.txt` covers the attached files.

| Component | Version | GitHub Release asset | Upstream source | SHA-256 |
|---|---:|---|---|---|
| Qt | 6.12.0 | `Qt-6.12.0-source.tar.xz` | [qt-everywhere-src-6.12.0.tar.xz](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz) | `98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165` |
| Qt for Python (PySide6 and Shiboken) | 6.12.0 | `PySide6-6.12.0-source.tar.xz` | [pyside-setup-everywhere-src-6.12.0.tar.xz](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz) | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` |

## The build contains

The current desktop build pins PySide6-Essentials and Shiboken **6.12.0**, which bundle Qt **6.12.0**. You can verify the version and actual files for a particular release in `BUILD_INFO.json` and `BUNDLED_NATIVE_COMPONENTS.json`; matching source archives and hashes are in `SOURCES.md` attached to the GitHub release.

## Where Qt is in the packages

| Package | Qt libraries | Qt plugins |
|---|---|---|
| macOS arm64 | `OnePyProject.app/Contents/Frameworks/PySide6/Qt/lib/Qt*.framework` | `OnePyProject.app/Contents/Frameworks/PySide6/Qt/plugins/` |
| Windows x64 | `OnePyProject/_internal/PySide6/Qt6*.dll` | `OnePyProject/_internal/PySide6/plugins/` |

The macOS app has top-level links such as `Contents/Frameworks/QtCore` that point into the PySide6 framework directory. Preserve the framework structure, symlinks, filenames, and relative locations. The Windows path is the current PyInstaller 6 one-folder layout. The release ZIP root contains `BUNDLED_NATIVE_COMPONENTS.json` with exact file paths and hashes; consult it if the packaging layout changes, and keep it with the original ZIP when moving the app elsewhere.

The macOS package currently contains QtCore, QtDBus, QtGui, QtNetwork, QtSvg, and QtWidgets frameworks, plus platform, style, TLS, SVG, image-format, and other plugins. The native inventory lists the exact files for each release.

## Prepare a compatible replacement

1. Download the original platform ZIP and keep an untouched backup.
2. Use the matching Qt/PySide source archives listed in `SOURCES.md`. Verify the archive hashes listed there and in the release's `SHA256SUMS.txt`.
3. Build shared libraries for the same operating system and architecture, with a compatible Qt ABI, module configuration, and plugin set. Prefer the exact Qt version in `BUILD_INFO.json`; a matching major/minor number by itself does not guarantee compatibility.
4. Close OnePyProject. Replace only the corresponding Qt shared libraries and any plugins you rebuilt, retaining the package's names and layout. Do not replace the PySide6 Python bindings or Shiboken with unrelated versions. Keep a backup so you can restore the original package.

## macOS

1. Copy `OnePyProject.app` to a writable location. Replace the relevant `Qt*.framework` bundles under `Contents/Frameworks/PySide6/Qt/lib/`. Keep each framework's `Versions` structure and the surrounding symlinks intact. If replacing plugins, put compatible plugin files under `Contents/Frameworks/PySide6/Qt/plugins/<plugin-type>/`.
2. The distributed app has an **ad-hoc** signature, not a Developer ID signature, and is not notarized. Changing a framework or plugin invalidates the app's sealed resource signature. For local testing, an ad-hoc re-sign can be attempted with:

   ```bash
   codesign --force --deep --sign - "/Applications/OnePyProject.app"
   codesign --verify --deep --strict "/Applications/OnePyProject.app"
   ```

This does not restore a publisher identity or notarization. macOS may still show a Gatekeeper warning or require approval to open the modified app. Apple's signing guidance recommends signing nested code from the inside out; `--deep --force` is a local repair shortcut, not a release-signing process.
3. Launch the modified app and test the UI, SVG icons, export, and the Qt functionality affected by the replacement.

## Windows

1. Extract the ZIP and make a backup of the whole `OnePyProject` folder.
2. Replace the matching `Qt6*.dll` files under `_internal/PySide6/` and, if rebuilt, the corresponding plugin DLLs under `_internal/PySide6/plugins/<plugin-type>/`. Preserve filenames and relative paths. Use `BUNDLED_NATIVE_COMPONENTS.json` to identify the shipped files.

