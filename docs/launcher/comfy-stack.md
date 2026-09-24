# ComfyUI / MiniMax H3 stack (stack `comfy`)

The launcher serves two independent workload stacks, selected with
`LAUNCHER_STACK` (or `--stack` / the GUI mode selector):

| Stack | What runs on the pod | Local result |
|---|---|---|
| `comfy` (default) | ComfyUI + the MiniMax H3 toolchain (this repository's installer) | the ComfyUI web UI (tunnel or direct) + LoRA/workflow management |
| `train` | the Fizgig training desktop (KasmVNC) | the desktop and the file manager, over one SSH tunnel — see [`train-stack.md`](train-stack.md) |

The `comfy` stack runs this repository's own pod-side installer (Apache-2.0,
see `NOTICE`): ComfyUI, PyTorch, the MiniMax H3 model weights
(preset-driven), Turbo LoRA, workflows, and optional SageAttention/Spectrum —
all driven by pod environment variables injected by the launcher at pod
creation.

## Usage

```powershell
# CLI
minimax-launcher start --stack comfy --preset dasiwa_mmh3v12
minimax-launcher status            # stack-aware (LAUNCHER_STACK or the registry)
minimax-launcher stop              # stops the registered pod of the selected stack
minimax-launcher doctor            # includes ComfyUI checks when the stack is comfy

# GUI
minimax-launcher gui               # mode selector: ComfyUI video / LoRA training
```

The stacks use **independent pods** (per-stack pod registry —
`docs/pod-provisioning.md`) and can be started/stopped independently; a
`RUNPOD_POD_ID` pod is never touched by any of them.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LAUNCHER_STACK` | `comfy` | Workload stack (`comfy` \| `train`) |
| `RUNPOD_COMFY_TEMPLATE_ID` | the public template `oa2vozqbum` | private RunPod template for the comfy stack |
| `COMFY_PRESET` | `dasiwa_mmh3v12` | comma-separated `H3_PRESETS` (what gets installed) |
| `COMFY_TIER` | `auto` | `auto` \| `light` \| `pruned` \| `pruned_scaled` \| `balanced` \| `max` |
| `COMFY_WORKFLOWS` | `all` | `all` or a comma subset of `t2v`, `i2v`, `r2v` |
| `COMFY_TURBO_LORA` | `true` | auto-download the Turbo LoRA |
| `COMFY_SAGE_ATTENTION` | `auto` | `auto` \| `true` \| `false` |
| `COMFY_SPECTRUM` | `true` | install Spectrum |
| `COMFY_ACCESS` | `tunnel` | `tunnel` (default) or `direct` (see below) |
| `COMFY_LOCAL_PORT` / `COMFY_REMOTE_PORT` | 8188 / 8188 | tunnel ports |
| `NTFY_TOPIC` | — | ntfy.sh topic for pod-side notifications (injected) |
| `PERSONAL_STORAGE_HF_REPO` | — | personal Hugging Face vault repo (injected) |

`RUNPOD_POD_ENV` (JSON) always overrides the above for the pod env. The
credential store also holds the ComfyUI template ID (the fifth field of
`minimax-launcher credentials set` / the GUI secrets dialog).

## Access modes

* **`tunnel` (default)** — `127.0.0.1:8188` is forwarded to the pod's ComfyUI
  over SSH; the UI runs in the local browser, all compute stays on the pod.
  Requires the pod to expose `22/tcp` and to run an SSH daemon (all images
  built by this repository do — see `docker/comfy/README.md`).
* **`direct` (explicit option only, never the default)** — the launcher uses
  the pod's public mapping of port 8188 instead of a tunnel and always logs a
  security warning: the ComfyUI endpoint has **no authentication**. The GUI
  asks for explicit confirmation before starting in this mode.

## Presets

`COMFY_PRESET` maps 1:1 to the installer's `H3_PRESETS` (the bash installer
remains the source of truth for what each preset installs):

* `dasiwa_mmh3v12` — the default door-to-door preset (DaSiWa MythicAlchemy
  C-MMH3 workflows + the matching model variant, auto tier).
* Other local presets from the installer (`aistudynow`, `minimaxh3auto_v5`,
  `muse_director`...) are selectable without further changes.

Changing the preset on an **existing** pod is supported: on the next `start`,
the launcher diffs the pod's managed env, and on a difference performs
stop → update-env → start (idempotent — an in-sync pod is never reconfigured).

## LoRA & personal storage

* The GUI LoRA panel installs/lists/removes LoRAs **on the pod** over
  SSH/SFTP (HF / CivitAI / direct-URL sources, `--personal` flag), using the
  same stored keys the CLI uses (`launcher/comfy_ops.py` wraps the installer's
  `install_lora.sh` / `uninstall.sh`).
* "Sync vault" pushes/pulls the personal Hugging Face repo
  (`PERSONAL_STORAGE_HF_REPO`, via the installer's `sync_push.sh`).
* Generated outputs (`/opt/ComfyUI/output/`) can be downloaded to a local
  folder (default `~/Downloads/comfy-outputs`), including nested
  subfolders — the H3 workflow templates pin prefixes such as
  `video/MiniMax_H3`, so downloads mirror that tree.
* `NTFY_TOPIC` (ntfy.sh) notifications are injected into the pod env when
  set; the pod-side installer publishes to that topic.

## The image

The public RunPod template the launcher deploys
(`oa2vozqbum`, https://console.runpod.io/hub/template/oa2vozqbum) runs the
image built from this repository's `Dockerfile`.
`image-contract.json` records what that image guarantees — paths,
endpoints, the pod-side scripts it ships, the CUDA floor and the
environment keys it consumes — and `tests/test_comfy_image_contract.py`
enforces it against the launcher's own constants, so a drift fails in CI
instead of surfacing as a silent pod-side misbehaviour.

## Validation

`tests/test_comfy_installer_regressions.py` pins the pod-side behaviours
that were learned the hard way (the boot time spent in `install.sh`, the
per-connection download limit, the checkpoint variant symlinks), and
`tests/test_comfy_ops.py` / `test_comfy_workflow.py` cover the
launcher-side operations and the UI-workflow → API-prompt conversion.
