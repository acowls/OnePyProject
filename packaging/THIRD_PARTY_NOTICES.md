# Third-party notices — OnePyProject desktop builds

These builds use Qt for Python (PySide6 Essentials and Shiboken) and Qt 6.12.0.
The intended choice for applicable Qt/PySide components is LGPL version 3.
Qt also contains third-party components with their own notices and licences.
The initial macOS build also collects Qt Network and its TLS plugins, plus
image-format and platform plugins; those need their own distribution audit.

- Qt licensing: https://doc.qt.io/qt-6/licensing.html
- Qt third-party notices: https://doc.qt.io/qt-6/licenses-used-in-qt.html
- LGPLv3: https://www.gnu.org/licenses/lgpl-3.0.html
- GPLv3 (incorporated by LGPLv3): https://www.gnu.org/licenses/gpl-3.0.html
- Qt/PySide source releases: https://download.qt.io/official_releases/
- pymppwriter 0.4.3, from the `acowls/pymppwriter` fork at commit 53e396b (pinned in `pyproject.toml`; `BUILD_INFO.json` records the source): MIT; its licence is included in `licenses/pymppwriter.txt`.
- olefile: BSD-style and PIL licences; included in `licenses/olefile.txt`.
- Icons8 Flat Color Icons: MIT option; included in `licenses/icons8.txt`.
- PyInstaller: GPL with a bootloader exception; included in `licenses/pyinstaller.txt`.
- OpenAI Python SDK: Apache 2.0; included in `licenses/openai.txt`.
- Python keyring: MIT; included in `licenses/keyring.txt`.
- Google Auth library: Apache 2.0; included in `licenses/google-auth.txt`.
- Requests: Apache 2.0; included in `licenses/requests.txt`.
- Additional SDK and credential-store dependencies have their own licences; their distribution metadata is included in the app and requires review.
- Python has its own licences: https://docs.python.org/3/license.html.

This is an incomplete inventory, **not a distribution compliance statement**.
Before sharing the binary, audit every bundled library and plugin, provide all
required licences and corresponding source or a valid offer, and verify that
recipients can replace the LGPL libraries. Signing may affect replacement.
OnePyProject itself is licensed under MIT; see `LICENSE`.
