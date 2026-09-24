"""Report how the vendored launcher differs from its upstream.

``NOTICE`` promises a resynchronisation procedure: "diff those modules against
that revision upstream, and the only intended differences are the provenance
header, the removal of the text-agent surface and the completion of the
English translation". This makes that check runnable instead of asserted.

Usage:
    python scripts/check_vendored_sync.py [path-to-openfox-forge]

Every changed line is classified:

* ``header``  — the ``Vendored from openfox-forge@<sha>`` banner;
* ``rename``  — the product rename this repository forces
  (``openfox-forge`` → ``minimax-launcher``, ``OPENFOX_FORGE_*`` →
  ``MINIMAX_LAUNCHER_*``, the product name, the bundled asset names);
* ``derived`` — anything else. Expected in the modules that were adapted
  (the agent surface removed, the cross-platform backends, the public
  template), and the reason this is a *report* and not a gate.

Exit status is non-zero only when a module upstream has no counterpart here —
that would mean the vendoring dropped something silently.
"""

from __future__ import annotations

import difflib
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parents[1]
VENDORED = HERE / "launcher"
DEFAULT_UPSTREAM = pathlib.Path("D:/OpenFox Forge/launcher")

HEADER = re.compile(r"Vendored from openfox-forge@|resynchronisation procedure")

RENAMES = (
    ("openfox-forge", "minimax-launcher"),
    ("OpenFox Forge", "MiniMax H3 Launcher"),
    ("OPENFOX_FORGE_STATE_DIR", "MINIMAX_LAUNCHER_STATE_DIR"),
    ("OPENFOX_FORGE_HOME", "MINIMAX_LAUNCHER_HOME"),
    ("OPENFOX_FORGE_ENV_FILE", "MINIMAX_LAUNCHER_ENV_FILE"),
    ("OPENFOX_NOTIFY_TITLE", "MINIMAX_NOTIFY_TITLE"),
    ("OPENFOX_NOTIFY_MESSAGE", "MINIMAX_NOTIFY_MESSAGE"),
    ("FORGE_STACK", "LAUNCHER_STACK"),
    ("FORGE_INFRA_RECOVERY", "LAUNCHER_INFRA_RECOVERY"),
    ("logo_openfoxforge_main.png", "logo_minimax_h3.png"),
    ("OpenFoxForge", "MiniMaxH3Launcher"),
)


def _is_rename(line: str) -> bool:
    """True when the only difference is one of the product renames."""
    return any(new in line for _, new in RENAMES) or any(
        old in line for old, _ in RENAMES
    )


def classify(upstream: str, vendored: str) -> dict[str, int]:
    counts = {"header": 0, "rename": 0, "derived": 0}
    for line in difflib.unified_diff(
        upstream.split("\n"), vendored.split("\n"), lineterm="", n=0
    ):
        if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
            continue
        body = line[1:].strip()
        if not body:
            continue
        if HEADER.search(body):
            counts["header"] += 1
        elif _is_rename(body):
            counts["rename"] += 1
        else:
            counts["derived"] += 1
    return counts


def main(argv: list[str]) -> int:
    upstream = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_UPSTREAM
    if not upstream.is_dir():
        print(f"upstream not found: {upstream}", file=sys.stderr)
        return 2

    missing = []
    rows = []
    for path in sorted(VENDORED.glob("*.py")):
        source = upstream / path.name
        if not source.is_file():
            missing.append(path.name)
            continue
        counts = classify(
            source.read_text(encoding="utf-8").replace("\r\n", "\n"),
            path.read_text(encoding="utf-8").replace("\r\n", "\n"),
        )
        rows.append((path.name, counts))

    print(f"upstream: {upstream}")
    print(f"{'module':<24}{'header':>8}{'rename':>8}{'derived':>9}")
    for name, counts in sorted(rows, key=lambda r: -sum(r[1].values())):
        print(
            f"{name:<24}{counts['header']:>8}{counts['rename']:>8}"
            f"{counts['derived']:>9}"
        )

    identical = [n for n, c in rows if sum(c.values()) <= c["header"]]
    print(
        f"\n{len(rows)} module(s) compared; "
        f"{len(identical)} differ by the provenance header alone."
    )
    if missing:
        print(f"MISSING upstream counterparts: {', '.join(missing)}")
        return 1
    print("Every vendored module has an upstream counterpart.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
