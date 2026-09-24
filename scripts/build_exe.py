"""Build the standalone MiniMax H3 Launcher binary (PyInstaller, one file).

Usage:
    python -m pip install ".[gui-build]"     # or: python -m pip install pyinstaller
    python scripts/build_exe.py              # -> dist/MiniMaxH3Launcher[.exe]
    python scripts/build_exe.py --target-arch universal2   # macOS only

Cross-platform on purpose — the release workflow builds on Windows, macOS
(arm64 + x64) and Linux — which means two platform details matter:

* ``--add-data`` takes ``SOURCE:DEST`` everywhere EXCEPT Windows, where it is
  ``SOURCE;DEST``. Hard-coding ``;`` makes every POSIX build die with
  "Wrong syntax, should be --add-data=SOURCE:DEST".
* ``--icon`` only means something to the Windows and macOS bootloaders, and
  each wants its own format (``.ico`` / ``.icns``). Linux has no such slot, so
  the flag is simply not passed there.

The tray icon is bundled when ``pystray`` (extra ``[tray]``) is installed in
the build environment; otherwise the build works fine without tray support.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Bundled as-is: the runtime resolves them through ``sys._MEIPASS``.
DATA_FILES = (
    ("launcher/icon.png", "launcher"),
    ("launcher/icon.ico", "launcher"),
    ("launcher/assets", "launcher/assets"),
)


def icon_for_this_platform() -> pathlib.Path | None:
    """The icon the bootloader wants here, or None when it wants none."""
    if os.name == "nt":
        return ROOT / "launcher" / "icon.ico"
    if sys.platform == "darwin":
        return ROOT / "launcher" / "icon.icns"
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-arch",
        default=None,
        choices=("arm64", "x86_64", "universal2"),
        help=(
            "macOS only: the architecture(s) to build for. universal2 needs a "
            "universal2 interpreter (what actions/setup-python installs)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "PyInstaller is not installed in this environment.\n"
            'Install it first:  python -m pip install ".[gui-build]"',
            file=sys.stderr,
        )
        return 1

    from PyInstaller.__main__ import run

    separator = os.pathsep
    args = [
        "--onefile",
        "--noconsole",
        "--clean",
        "--name",
        "MiniMaxH3Launcher",
    ]

    if options.target_arch is not None:
        if sys.platform != "darwin":
            print(
                "--target-arch only applies to macOS builds",
                file=sys.stderr,
            )
            return 1
        args += ["--target-architecture", options.target_arch]

    icon = icon_for_this_platform()
    if icon is not None:
        if not icon.is_file():
            print(f"missing icon: {icon}", file=sys.stderr)
            return 1
        args += ["--icon", str(icon)]

    for source, destination in DATA_FILES:
        path = ROOT / source
        if not path.exists():
            print(f"missing data file: {path}", file=sys.stderr)
            return 1
        # PyInstaller wants SOURCE<sep>DEST, and <sep> is os.pathsep.
        args += ["--add-data", f"{source}{separator}{destination}"]

    # The locale tables are imported by name at runtime (see
    # launcher.i18n._load_table), which PyInstaller cannot see: without these
    # the frozen build would silently fall back to English.
    args += ["--hidden-import", "launcher.locales"]
    args += ["--hidden-import", "launcher.locales.fr"]

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

    args.append("launcher_gui_entry.py")
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
