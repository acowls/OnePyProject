# Third-party notices — OnePyProject desktop builds

OnePyProject's own code is licensed under the MIT License; see `LICENSE`. The toolbar icons are Icons8 Flat Color Icons, used under its MIT option; the copyright and license text are in `licenses/icons8.txt`.

## Python and build-tool components

Each archive and app bundle contains a generated inventory of the Python distributions detected in the frozen app. Their declared license metadata and license/notice files collected from their `.dist-info` or `.egg-info` records are listed in the table below and copied under `licenses/python-packages/`. The bundled Python runtime's license is under `licenses/Python-*/`. PyInstaller and hook notices are under `licenses/build-tools/`.

## Qt for Python and Qt

The app bundles PySide6-Essentials, Shiboken, Qt shared libraries, and platform, TLS, SVG, and image-format plugins. Copies of LGPL-3.0, GPL-2.0, and GPL-3.0 are in `licenses/`; the PySide6/Shiboken wheel metadata offers LGPL-3.0-only, GPL-2.0-only, or GPL-3.0-only licensing. Qt/PySide6 and their included third-party components have component-specific licensing. The release ZIP root contains `BUNDLED_NATIVE_COMPONENTS.json` with the exact native-file paths and hashes; Qt/PySide source archives and official licensing references are documented in `SOURCES.md`.

The native-file list is an artifact inventory, not a complete attribution analysis of code statically incorporated inside Qt libraries. Review the upstream Qt and Qt for Python third-party license/SBOM information for the exact modules and plugins shipped. In particular, verify that every Qt component used is available under a license compatible with the app's distribution and that any LGPL requirements are met. Practical replacement notes are in `REPLACING_QT.md`.

## Notices

- Qt licensing: [https://doc.qt.io/qt-6/licensing.html](https://doc.qt.io/qt-6/licensing.html)
- Qt third-party notices: [https://doc.qt.io/qt-6/licenses-used-in-qt.html](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- LGPLv3: [https://www.gnu.org/licenses/lgpl-3.0.html](https://www.gnu.org/licenses/lgpl-3.0.html)
- GPLv3 (incorporated by LGPLv3): [https://www.gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html)
- Qt/PySide source releases: [https://download.qt.io/official_releases/](https://download.qt.io/official_releases/)
- pymppwriter 0.4.3, from the `acowls/pymppwriter` fork at commit 53e396b (pinned in `pyproject.toml`; `BUILD_INFO.json` records the source): MIT; its licence is included in `licenses/pymppwriter.txt`.
- olefile: BSD-style and PIL licences; included in `licenses/olefile.txt`.
- Icons8 Flat Color Icons: MIT option; included in `licenses/icons8.txt`.
- PyInstaller: GPL with a bootloader exception; included in `licenses/pyinstaller.txt`.
- OpenAI Python SDK: Apache 2.0; included in `licenses/openai.txt`.
- Python keyring: MIT; included in `licenses/keyring.txt`.
- Google Auth library: Apache 2.0; included in `licenses/google-auth.txt`.
- Requests: Apache 2.0; included in `licenses/requests.txt`.
- Additional SDK and credential-store dependencies have their own licences; their distribution metadata is included in the app and requires review.
- Python has its own licences: [https://docs.python.org/3/license.html](https://docs.python.org/3/license.html).
