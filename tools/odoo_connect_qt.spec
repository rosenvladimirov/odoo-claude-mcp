# PyInstaller spec — cross-platform build for Odoo Connect Manager (Qt/PySide6).
# Usage:
#   pyinstaller tools/odoo_connect_qt.spec
# Windows artefact: dist/OdooConnect.exe
# Linux artefact:   dist/OdooConnect
# macOS artefact:   dist/OdooConnect.app
#
# Prerequisites:
#   pip install pyinstaller PySide6 requests paramiko
#
# Automated builds run via .github/workflows/build-gui.yml on every
# push to main. Artefacts are attached to the workflow run.

block_cipher = None

a = Analysis(
    ["odoo_connect_qt.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "paramiko",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim the wheel — we do NOT ship QtWebEngine / QtQuick.
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtMultimedia",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtPositioning",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="OdooConnect",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # GUI app — no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,       # TODO: add icon.ico after branding
)
