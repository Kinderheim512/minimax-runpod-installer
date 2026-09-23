# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MiniMax H3 Launcher (one file, windowed).

The bundled data is what the frozen build cannot find by itself:

* ``launcher/icon.png`` / ``icon.ico`` — the window and taskbar icon;
* ``launcher/assets`` — the icon set the GUI resolves at runtime;
* the locale tables, imported by name (``launcher.i18n._load_table``), which
  PyInstaller cannot see statically.

``ttkbootstrap`` is collected when it is installed: the GUI falls back to the
hand-rolled palette without it, so the build works either way.
"""
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ('launcher/icon.png', 'launcher'),
    ('launcher/icon.ico', 'launcher'),
    ('launcher/assets', 'launcher/assets'),
]
datas += collect_data_files('ttkbootstrap')

a = Analysis(
    ['launcher_gui_entry.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'launcher.locales',
        'launcher.locales.fr',
        'pystray._win32',
        'pystray._appindicator',
        'pystray._xorg',
    ],
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
    name='MiniMaxH3Launcher',
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
    icon=['launcher/icon.ico'],
)
