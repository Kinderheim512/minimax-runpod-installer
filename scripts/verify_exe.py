"""Verify the packaged exe on its actual bytecode.

A raw byte search over a PyInstaller one-file build only finds the archive's
table of contents: the modules themselves are zlib-compressed inside the PYZ,
so `b"oa2vozqbum" in exe.read_bytes()` is False even when the constant is
there. This reads the archive the way the bootloader does — open the PYZ,
decompress each module, marshal-load it and walk its code objects — and
asserts the shipped constants really are inside.
"""

from __future__ import annotations

import pathlib
import sys
import zlib

from PyInstaller.archive.readers import CArchiveReader


def iter_code_constants(code):
    """Yield every string constant reachable from *code*."""
    stack = [code]
    seen = 0
    while stack:
        current = stack.pop()
        seen += 1
        if seen > 200_000:
            return
        for const in current.co_consts:
            if isinstance(const, str):
                yield const
            elif hasattr(const, "co_consts"):
                stack.append(const)


def main() -> int:
    exe = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dist/MiniMaxH3Launcher.exe")
    print(f"exe: {exe} ({exe.stat().st_size:,} bytes)")

    reader = CArchiveReader(str(exe))
    names = list(reader.toc)
    print(f"archive entries: {len(names)}")

    pyz_name = next((n for n in names if n.endswith(".pyz")), None)
    if pyz_name is None:
        print("MISS no PYZ in the archive")
        return 1
    print(f"pyz: {pyz_name}")

    pyz = reader.open_embedded_archive(pyz_name)
    modules = sorted(pyz.toc)
    print(f"bundled modules: {len(modules)}")

    required_modules = [
        "launcher.gui",
        "launcher.config",
        "launcher.credentials",
        "launcher.orchestrator",
        "launcher.i18n",
        "launcher.locales",
        "launcher.locales.fr",
        "launcher.comfy_ops",
        "launcher.comfy_watch",
        "launcher.train_ops",
    ]
    missing_modules = [m for m in required_modules if m not in modules]
    for name in required_modules:
        print(f"  {'OK ' if name in modules else 'MISS'} {name}")

    # Collect every string constant of the modules that matter.
    strings: set[str] = set()
    for name in modules:
        if name != "launcher" and not name.startswith("launcher."):
            continue
        try:
            # ``extract`` already unmarshals: it hands back the code object.
            code = pyz.extract(name)
        except Exception:  # noqa: BLE001 - a data module has no code object
            continue
        if hasattr(code, "co_consts"):
            strings.update(iter_code_constants(code))

    print(f"launcher string constants: {len(strings):,}")

    required_strings = [
        "oa2vozqbum",
        "minimax-launcher",
        "MiniMax H3 Launcher",
        "gui.json",
        "MINIMAX_LAUNCHER_CREDENTIAL_BACKEND",
        "MINIMAX_LAUNCHER_CREDENTIALS_DIR",
        "MINIMAX_LAUNCHER_ENV_FILE",
        "MINIMAX_LAUNCHER_STATE_DIR",
        "LAUNCHER_STACK",
        "2.0.0",
    ]
    missing_strings = [s for s in required_strings if s not in strings]
    for value in required_strings:
        print(f"  {'OK ' if value in strings else 'MISS'} {value!r}")

    # The removed surface must not be in the build at all.
    banned = [
        "rfv75gjaip",
        "agent_cli",
        "searxng",
        "LlmConfig",
        "llamacpp",
        "OPENFOX_FORGE_ENV_FILE",
    ]
    present = [b for b in banned if b in strings]
    print("removed surface present:", present or "none")

    problems = missing_modules + missing_strings + present
    if problems:
        print("\nFAILED:", problems)
        return 1
    print("\nOK: the packaged bytecode carries the launcher's current state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
