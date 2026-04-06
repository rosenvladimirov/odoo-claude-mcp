#!/bin/bash
# Build Windows .exe + installer from Linux using Docker
# Usage: ./packaging/windows/build.sh

set -e
cd "$(dirname "$0")/../.."
PROJECT_DIR="$(pwd)"

echo "=== Building Odoo Connection Manager for Windows ==="

# Create temp build context
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

cp tools/odoo_connect_qt.py "$BUILD_DIR/odoo_connect.py"

# Create requirements file
cat > "$BUILD_DIR/requirements.txt" << 'EOF'
PySide6>=6.6.0
EOF

# Create PyInstaller spec
cat > "$BUILD_DIR/odoo_connect.spec" << 'SPEC'
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['odoo_connect.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'PIL'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OdooConnect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)
SPEC

# Create entrypoint for Docker
cat > "$BUILD_DIR/build_inside.sh" << 'BUILDEOF'
#!/bin/bash
set -e
cd /src
pip install PySide6 pyinstaller
pyinstaller --clean --noconfirm odoo_connect.spec
cp dist/OdooConnect.exe /output/
echo "Build complete: OdooConnect.exe"
BUILDEOF
chmod +x "$BUILD_DIR/build_inside.sh"

# Create output dir
mkdir -p "$PROJECT_DIR/packaging/windows/dist"

echo "Running PyInstaller in Docker (Wine)..."
docker run --rm \
    -v "$BUILD_DIR:/src" \
    -v "$PROJECT_DIR/packaging/windows/dist:/output" \
    cdrx/pyinstaller-windows:python3 \
    bash /src/build_inside.sh

# Check result
if [ -f "$PROJECT_DIR/packaging/windows/dist/OdooConnect.exe" ]; then
    SIZE=$(du -sh "$PROJECT_DIR/packaging/windows/dist/OdooConnect.exe" | cut -f1)
    echo ""
    echo "=== SUCCESS ==="
    echo "Output: packaging/windows/dist/OdooConnect.exe ($SIZE)"
else
    echo "ERROR: Build failed"
    exit 1
fi
