# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for macOS (.app bundle)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('FileDuplicator.icns', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FileDuplicator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['FileDuplicator.icns'],
)

app = BUNDLE(
    exe,
    name='FileDuplicator.app',
    icon='FileDuplicator.icns',
    bundle_identifier='com.fileduplicator.app',
    info_plist={
        'CFBundleShortVersionString': '1.2.0',
        'CFBundleName': 'FileDuplicator',
        'CFBundleDisplayName': 'File Duplicator',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
    },
)
