# Third-party notices — internal test builds

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
- pymppwriter 0.4.3: MIT; its licence is included in `licenses/pymppwriter.txt`.
- olefile: BSD-style and PIL licences; included in `licenses/olefile.txt`.
- Icons8 Flat Color Icons: MIT option; included in `licenses/icons8.txt`.
- PyInstaller: GPL with a bootloader exception; included in `licenses/pyinstaller.txt`.
- Python has its own licences: https://docs.python.org/3/license.html.

This is an incomplete inventory, **not a distribution compliance statement**.
Before sharing the binary, audit every bundled library and plugin, provide all
required licences and corresponding source or a valid offer, and verify that
recipients can replace the LGPL libraries. Signing may affect replacement.
OnePyProject's own licence has not yet been chosen.
