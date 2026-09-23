"""Build the standalone MiniMaxH3Launcher.exe (PyInstaller, onefile, windowed).

Usage:
    python -m pip install ".[gui-build]"     # or: python -m pip install pyinstaller
    python scripts/build_exe.py              # -> dist/MiniMaxH3Launcher.exe

The tray icon is bundled when ``pystray`` (extra [tray]) is installed in the
build environment; otherwise the exe builds fine without tray support.
"""

from __future__ import annotations

import importlib.util
import sys


def main() -> int:
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "PyInstaller is not installed in this environment.\n"
            "Install it first:  python -m pip install \".[gui-build]\"",
            file=sys.stderr,
        )
        return 1

    from PyInstaller.__main__ import run

    args = [
        "--onefile",
        "--noconsole",
        "--clean",
        "--name",
        "MiniMaxH3Launcher",
        "--icon",
        "launcher/icon.ico",
        "--add-data",
        "launcher/icon.png;launcher",
        "--add-data",
        "launcher/icon.ico;launcher",
        # Bundled icons: resolved through sys._MEIPASS at runtime (see
        # launcher.gui._asset_icon_path). Without this the frozen build
        # silently falls back to the text/glyph rendering.
        "--add-data",
        "launcher/assets;launcher/assets",
        # The locale tables are imported by name at runtime (see
        # launcher.i18n._load_table), which PyInstaller cannot see: without
        # these the frozen build would silently fall back to English.
        "--hidden-import",
        "launcher.locales",
        "--hidden-import",
        "launcher.locales.fr",
        "launcher_gui_entry.py",
    ]
    if importlib.util.find_spec("pystray") is not None:
        # pystray selects its backend with a dynamic import.
        for backend in ("_win32", "_appindicator", "_xorg"):
            args += ["--hidden-import", f"pystray.{backend}"]
    if importlib.util.find_spec("ttkbootstrap") is not None:
        # ttkbootstrap keeps widget/element assets next to its modules;
        # --collect-data bundles them so a frozen build keeps the ready-made
        # themes. Without the package the GUI falls back to the hand-rolled
        # palettes and builds fine.
        args += ["--collect-data", "ttkbootstrap"]
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
