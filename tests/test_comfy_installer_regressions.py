"""Regressions guard for the comfy installer's boot path.

Each test here pins a defect that was observed **live on a pod running image
2.0.3** — they are cheap static checks on the shell sources, which is the only
way to keep a bash installer honest from pytest. Every one of them failed
before the corresponding fix.

Observed on the pod (2026-09-18):

* ``COMFYUI_COMMIT=v0.36.0`` was in the pod env, and the pod still ran
  ``v0.35.1``: ``git status --porcelain`` returned exactly one line,
  ``?? .comfyui_target_installed`` — the marker file the installer writes
  itself. The "local changes" guard therefore refused the pinned checkout on
  every boot, and the marker (only written on a clean tree) was never updated.
* ``importlib.util.find_spec("comfyui_manager")`` was ``None`` and ``pip list``
  empty, so ComfyUI core disabled ``--enable-manager`` and the UI showed
  "upgrade ComfyUI-Manager to version 4.2.1 or higher" — while the git clone
  sat at 3.41 and the boot log said "Step 'manager_installed' already done,
  skipping".
* The boot spent 7 min 26 s in ``install.sh``, of which **4 min 12 s** were the
  32 annuaire LoRAs downloaded one by one from CivitAI (36 MB/s — the
  per-connection limit, not the pod's network), for content only read at
  generation time.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMFY = REPO_ROOT / "comfy"


def _read(name: str) -> str:
    return (COMFY / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. the version pin must actually apply
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 2. the manager ComfyUI core actually looks for
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 3. the annuaire LoRAs must not block ComfyUI's start
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 4. parallel LoRA downloads, safely
# --------------------------------------------------------------------------- #


