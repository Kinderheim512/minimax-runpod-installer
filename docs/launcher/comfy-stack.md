# ComfyUI / MiniMax H3 stack (stack `comfy`)

The launcher serves three independent workload stacks, selected with
`FORGE_STACK` (or `--stack` / the GUI mode selector):

| Stack | What runs on the pod | Local result |
|---|---|---|
| `agent` (default) | vLLM serving Qwen3.8-27B (see the README) | OpenFox pointed at the tunneled model |
| `comfy` | ComfyUI + the MiniMax H3 toolchain (the `comfy/` installer) | the ComfyUI web UI (tunnel or direct) + LoRA/workflow management |
| `llamacpp` | `llama-server` (llama.cpp) serving a GGUF quant (+ mmproj vision) | OpenFox pointed at the tunneled model — see [`llamacpp-stack.md`](llamacpp-stack.md) |

The `comfy` stack runs the [MiniMax H3 RunPod installer](./README.md) merged
into this repository under `comfy/` (Apache-2.0, see `NOTICE`): ComfyUI,
PyTorch, the MiniMax H3 model weights (preset-driven), Turbo LoRA, workflows,
and optional SageAttention/Spectrum — all driven by pod environment variables
injected by the launcher at pod creation.

## Usage

```powershell
# CLI
openfox-forge start --stack comfy --preset dasiwa_mmh3v12
openfox-forge status            # stack-aware (FORGE_STACK or the registry)
openfox-forge stop              # stops the registered pod of the selected stack
openfox-forge doctor            # includes ComfyUI checks when the stack is comfy

# GUI
openfox-forge gui               # mode selector: Agent-texte / Vidéo-Comfy / GGUF llama.cpp
```

The stacks use **independent pods** (per-stack pod registry —
`docs/pod-provisioning.md`) and can be started/stopped independently; a
`RUNPOD_POD_ID` pod is never touched by any of them.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FORGE_STACK` | `agent` | Workload stack (`agent` \| `comfy`) |
| `RUNPOD_COMFY_TEMPLATE_ID` | (credential store) | private RunPod template for the comfy stack |
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
`openfox-forge credentials set` / the GUI secrets dialog).

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

## MCP tools (agent-facing)

* `forge_comfy_status` — pod status, ComfyUI health (`/system_stats`), VRAM,
  queue depth, preset.
* `forge_comfy_generate` — trigger a MiniMax H3 generation through the
  ComfyUI API (`/prompt` + `/api/queue`) on the registered comfy pod.
  Workflows pin a model file per tier; if the pod serves a different
  quantization of the same model, the loader names are adapted to the
  server's actual files (reported in the tool output).

Both reuse the shared primitives (`RunPodClient`, `PodRegistry`,
`ComfyOps`); nothing is duplicated from the CLI/GUI paths.

## The image

See `docker/comfy/README.md`: the image is built from `comfy/` (single
source of truth), published only as **versioned tags**
(`kinderheim512/openfox-forge-comfy:<version>` on Docker Hub + GHCR), and
**never** as `:latest` (the community float
`kinderheim512/minimax-h3-comfyui:latest` stays the reference for existing
RunPod templates).

## Validation

The dasiwa door-to-door end-to-end run on the frozen community image
(`sha256:542ee909…`) is documented in `docs/c2a-validation.md`, including
the RunPod template history (`r7ieeeyfds` → `b10i658px4` → `p0dbln2sk4`)
and the two infrastructure facts it surfaced (digest-pinned v2 templates
cannot create pods; RunPod `http` port mappings are not internet-reachable).

The C2b complement — preset → pod-env mapping, the live stop → env update →
start sync cycle, the explicit `direct` option, and the ComfyUI lines in
`doctor`/`status` — is documented in `docs/c2b-validation.md`.
