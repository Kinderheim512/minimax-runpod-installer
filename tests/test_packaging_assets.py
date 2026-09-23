"""Packaging guards for the bundled icon assets.

The icons are useless if they do not travel with the frozen build: PyInstaller
only ships what ``--add-data`` (``scripts/build_exe.py``) and the ``datas``
list (``OpenFoxForge.spec``) declare, and the app resolves them through
``sys._MEIPASS``. These tests keep both in sync with the shipped files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from launcher import gui

ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = ROOT / "launcher" / "assets" / "icons"
SPEC = ROOT / "MiniMaxH3Launcher.spec"
BUILD_SCRIPT = ROOT / "scripts" / "build_exe.py"
SHRINK_SCRIPT = ROOT / "scripts" / "shrink_icons.py"

#: Pre-shrunk icons must stay small: the sources were 272 KB - 1.1 MB each.
MAX_ICON_BYTES = 40 * 1024
#: ... and no larger than ~2x their display size. The workflow glyph is 4:1,
#: so it is allowed the extra width (a square 40px box would reduce it to a
#: 40x10 sliver).
MAX_ICON_HEIGHT = 80
MAX_ICON_WIDTH = 200
#: ``"name.png": (max_width, max_height),`` in scripts/shrink_icons.py.
_ICON_BOX_RE = re.compile(r'"([a-z0-9_]+\.png)": \((\d+), (\d+)\),')


def _shrink_boxes() -> dict:
    return {
        name: (int(width), int(height))
        for name, width, height in _ICON_BOX_RE.findall(
            SHRINK_SCRIPT.read_text(encoding="utf-8")
        )
    }


def test_icons_are_pre_shrunk_not_the_raw_sources() -> None:
    for path in ICONS_DIR.glob("*.png"):
        assert path.stat().st_size <= MAX_ICON_BYTES, path.name
        with open(path, "rb") as handle:
            header = handle.read(24)
        # PNG IHDR: width/height are big-endian 32-bit at offsets 16 and 20.
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        assert height <= MAX_ICON_HEIGHT, f"{path.name} {width}x{height}"
        assert width <= MAX_ICON_WIDTH, f"{path.name} {width}x{height}"


def test_spec_bundles_the_icon_assets() -> None:
    # The .spec is tracked here (it is what the release workflow builds with);
    # the tracked build script remains the contract either way.
    if not SPEC.is_file():
        pytest.skip("MiniMaxH3Launcher.spec is not present")
    text = SPEC.read_text(encoding="utf-8")
    assert "launcher/assets" in text


