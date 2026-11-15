import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


root = Path(SPECPATH).parent
datas = [
    (str(root / "onepyproject" / "assets"), "onepyproject/assets"),
    (str(root / "packaging" / "THIRD_PARTY_NOTICES.md"), "."),
    (str(root / "LICENSE"), "."),
]
datas += collect_data_files("pymppwriter")
datas += copy_metadata("pymppwriter") + copy_metadata("olefile")
datas += copy_metadata("openai", recursive=True) + copy_metadata("keyring", recursive=True)

hiddenimports = ["PySide6.QtSvg"]
if sys.platform == "darwin":
    hiddenimports.append("keyring.backends.macOS")
elif sys.platform == "win32":
    hiddenimports.append("keyring.backends.Windows")

a = Analysis(
    [str(root / "packaging" / "frozen_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OnePyProject",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OnePyProject",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="OnePyProject.app",
        bundle_identifier="com.onepyproject.OnePyProject",
    )
