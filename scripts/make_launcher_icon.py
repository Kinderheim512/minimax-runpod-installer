"""Generate the launcher's icon set from one master PNG.

Usage:
    python scripts/make_launcher_icon.py <master.png> [--out launcher]

The master is the delivered 1254x1254 app-icon artwork. Three artefacts are
written next to ``launcher/icon.png``:

* ``icon.png``  — 512x512 RGBA, what Tk paints (``iconphoto``);
* ``icon.ico``  — Windows, 16/24/32/48/64/128/256 in one file, so the taskbar
  and the Alt-Tab switcher each pick the size they need;
* ``icon.icns`` — macOS, written here rather than with ``iconutil`` so the
  macOS release can be built by CI on a machine that never sees this one.

The 1254x1254 master is deliberately NOT committed as ``icon.png``: it is
~1.8 MB of pixels for a 40 px header badge and a window icon, and it ends up
in every frozen build.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from PIL import Image

#: Sizes packed into the Windows ``.ico``.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: ``.icns`` entry type -> square size. The modern PNG-bearing types are the
#: only ones a current macOS reads; the ``@2x`` ones are what Retina picks.
ICNS_ENTRIES = (
    (b"ic11", 32),  # 16x16@2x
    (b"ic12", 64),  # 32x32@2x
    (b"ic07", 128),  # 128x128
    (b"ic13", 256),  # 128x128@2x
    (b"ic08", 256),  # 256x256
    (b"ic14", 512),  # 256x256@2x
    (b"ic09", 512),  # 512x512
    (b"ic10", 1024),  # 512x512@2x
)

PNG_SIZE = 512


def build_icns(master: Image.Image) -> bytes:
    """Assemble an ``.icns`` container out of PNG-encoded entries."""
    body = b""
    for kind, size in ICNS_ENTRIES:
        buffer = _png_bytes(master, size)
        # 4-byte type + 4-byte total length (header included) + payload.
        body += kind + struct.pack(">I", len(buffer) + 8) + buffer
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def _png_bytes(master: Image.Image, size: int) -> bytes:
    import io

    buffer = io.BytesIO()
    _resize(master, size).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _resize(master: Image.Image, size: int) -> Image.Image:
    return master.resize((size, size), Image.LANCZOS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path, help="source PNG (square)")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("launcher"),
        help="directory that receives icon.png / icon.ico / icon.icns",
    )
    parser.add_argument(
        "--icns",
        action="store_true",
        help="also write icon.icns (macOS packaging only; it is ~2 MB)",
    )
    args = parser.parse_args(argv)

    if not args.master.is_file():
        print(f"master icon not found: {args.master}", file=sys.stderr)
        return 1

    master = Image.open(args.master).convert("RGBA")
    if master.width != master.height:
        print(
            f"master icon must be square, got {master.width}x{master.height}",
            file=sys.stderr,
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    png_path = args.out / "icon.png"
    _resize(master, PNG_SIZE).save(png_path, format="PNG", optimize=True)

    ico_path = args.out / "icon.ico"
    _resize(master, 256).save(
        ico_path, format="ICO", sizes=[(size, size) for size in ICO_SIZES]
    )
    written = [png_path, ico_path]

    if args.icns:
        icns_path = args.out / "icon.icns"
        icns_path.write_bytes(build_icns(master))
        written.append(icns_path)

    for path in written:
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
