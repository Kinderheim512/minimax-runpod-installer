# 🚀 MiniMax H3 RunPod Installer

<p align="center">

**Guided or one-command deployment of ComfyUI + MiniMax H3 on RunPod**

Installs ComfyUI, MiniMax H3, official workflows, and dependencies, and
tunes everything for the GPU it detects.

</p>

<p align="center">

![Platform](https://img.shields.io/badge/Platform-RunPod-blue)
![ComfyUI](https://img.shields.io/badge/ComfyUI-%3E%3D0.30.0-green)
![Python](https://img.shields.io/badge/Python-3.10%2B-yellow)
![CUDA](https://img.shields.io/badge/CUDA-auto--detected%20(11.8–13.0)-success)
![License](https://img.shields.io/github/license/Kinderheim512/minimax-runpod-installer)

</p>

---

## Contents

- [Features](#-features)
- [Quick start](#-quick-start)
- [The wizard, in detail](#-the-wizard-in-detail)
- [Model tiers](#-model-tiers-h3_tier)
- [Workflows](#-workflows)
- [Presets](#-presets-extra-models-for-a-specific-workflow)
- [Installed components](#-installed-components)
- [Pinning a ComfyUI commit](#-pinning-a-comfyui-commit-reproducible-installs)
- [Installing and managing LoRAs](#-installing-and-managing-loras)
- [Spectrum MiniMax H3](#-spectrum-minimax-h3-optional)
- [Surviving web-terminal disconnects](#-surviving-web-terminal-disconnects-tmux)
- [CLI reference](#-cli-reference)
- [GPU support](#-gpu-support)
- [Project structure](#-project-structure)
- [Documentation](#-documentation)
- [Roadmap](#-roadmap)

---

## ✨ Features

* 🧙 **Interactive setup wizard** (`wizard.sh`) — answer a few questions
  (preset, tier, Turbo LoRA, SageAttention, Spectrum), get a recap, confirm,
  done
* 🚀 One-command **non-interactive** installation too (`bootstrap.sh`),
  for scripted/repeat deployments
* 🎬 Automatic ComfyUI installation, tracking its default branch
* ⚡ Automatic GPU/CUDA detection and matching PyTorch build install
* 🧠 **Five** MiniMax H3 weight tiers, auto-picked to fit your VRAM (or
  forced)
* 🧩 **Presets** — self-contained model+workflow bundles for specific
  community workflows, on top of (or instead of) the standard tiers
* 🎯 Workflow-aware installs — download only what your selected workflows
  need
* 📥 Smart model downloader: resume, integrity checks, automatic repair
* 📏 Disk-space estimated and checked *before* any download starts
* 📦 Automatic installation of the official MiniMax H3 workflows
* 🎨 LoRA manager: install, list, and remove from the command line
* 🌐 Hugging Face and CivitAI as model sources
* 📈 Automatic GPU-based launch flag tuning (`--highvram`,
  `--disable-smart-memory`, `--fast`, attention backend, ...)
* 🖇 Automatic persistent tmux session, survives web-terminal disconnects
* 🔄 Idempotent update system — safe to re-run any time
* 📋 Non-destructive verification (`check.sh`)
* 🖥 RunPod-first, but works on any Linux pod with an NVIDIA GPU

---

## ⚡ Quick start

Create a fresh RunPod (PyTorch / CUDA / Ubuntu template, NVIDIA GPU, port
`8188` exposed as HTTP), open its terminal, and clone the project:

```bash
cd /workspace
git clone https://github.com/Kinderheim512/minimax-runpod-installer.git
cd minimax-runpod-installer
```

Then pick one of the two entry points below.

### Option A — Guided (recommended)

```bash
bash wizard.sh
```

Answers a handful of questions — preset, model tier, Turbo LoRA,
SageAttention, Spectrum — shows a recap before doing anything, then installs
and offers to launch ComfyUI in tmux right away. Best default for a first
install, or whenever you want to change something without memorizing flags.
See [The wizard, in detail](#-the-wizard-in-detail) below for a full sample
run.

### Option B — One command, no questions asked

```bash
bash bootstrap.sh
```

Uses whatever is set in `config.env` (the `dasiwa_mmh3v12` preset by
default) with **zero prompts** — picks a matching PyTorch/CUDA build,
installs ComfyUI + ComfyUI-Manager + custom nodes, downloads the selected
preset/tier, installs the workflows, tunes launch flags for the detected
GPU, and starts ComfyUI **inside a persistent tmux session** (see
[TMUX.md](TMUX.md)). Best for repeat/scripted deployments, or once you
already know exactly what you want.

Either way, no manual configuration is required for a first run. For
anything beyond the defaults, see [CLI reference](#-cli-reference) below, or
the full walkthrough in [docs/INSTALL_EN.md](docs/INSTALL_EN.md) /
[docs/INSTALL_FR.md](docs/INSTALL_FR.md).

---

## 🧙 The wizard, in detail

`bash wizard.sh` asks, in order — Enter alone always keeps the shown
default:

| Question | Options |
|-|-|
| Preset | None (standard tiers) · `dasiwa_mmh3v12` **(default)** · `aistudynow` · `minimaxh3auto_v5` |
| Model tier *(skipped if the preset replaces it)* | `auto` **(default)** · `light` · `pruned` · `pruned_scaled` · `balanced` · `max` |
| Turbo LoRA | On **(default)** · Off |
| SageAttention | On **(default)** · Off |
| Spectrum | On **(default)** · Off |

A preset that fully replaces the standard tier (currently only
`dasiwa_mmh3v12` — see [Presets](#-presets-extra-models-for-a-specific-workflow))
skips the tier question entirely, since it wouldn't do anything. Sample run:

```
┌────────────────────────────────────────────────────┐
│  Assistant de configuration — MiniMax H3            │
└────────────────────────────────────────────────────┘
(Entrée seule = garder le choix par défaut à chaque question)
Preset (jeu de modèles/workflow) :
 1) Aucun — installation standard uniquement
 2) dasiwa_mmh3v12 — DaSiWa MythicAlchemy (remplace le palier standard)  [défaut]
 3) aistudynow — checkpoint expérimental W4A8 (additif)
 4) minimaxh3auto_v5 (additif)
Choix [1-4, Entrée = défaut] :
→ 'dasiwa_mmh3v12' fournit son propre jeu de poids et son propre workflow.
Turbo LoRA MiniMax H3 (génération accélérée) :
 1) Activé (téléchargement + custom node auto)  [défaut]
 2) Désactivé
Choix [1-2, Entrée = défaut] :
...
Récapitulatif :
 - Preset       : dasiwa_mmh3v12
 - Palier       : (n/a — fourni par le preset)
 - Turbo LoRA   : on
 - SageAttention: on
 - Spectrum     : on
Lancer l'installation avec ces réglages ? [O/n]
```

After install, it asks once more before starting ComfyUI:

```
Lancer ComfyUI maintenant (session tmux) ? [O/n]
```

Answer `n` and start it later with `bash launch.sh --tmux` whenever you're
ready. You can also reach the wizard from the interactive menu:

```bash
bash menu.sh   # option 0
```

---

## 🧠 Model tiers (`H3_TIER`)

MiniMax H3 ships at five quality/size tiers. `install.sh` downloads only
one tier — the one you select — never all five.

|Tier|Min. VRAM|Diffusion model (FL2VA or REF2VA)|Text encoder|Approx. total*|
|-|-|-|-|-|
|`light`|8 GB|INT4Q mixed INT4/INT8 ConvRot, ~18.5 GB|NVFP4 AWQ, ~15.7 GB|~37 GB|
|`pruned`|24 GB|pruned INT8 ConvRot, ~21 GB — **official recommendation**|NVFP4 AWQ, ~15.7 GB|~40 GB|
|`pruned_scaled`|24 GB|pruned FP8 scaled, ~21 GB — fallback, manual only|NVFP4 AWQ, ~15.7 GB|~40 GB|
|`balanced`|40 GB|pruned BF16, ~40.2 GB — full precision, no lossy quant|NVFP4 AWQ, ~15.7 GB|~59 GB|
|`max`|48 GB|BF16, ~66.3 GB, not pruned|BF16, ~51.5 GB|~121 GB|

`light` is the only tier whose diffusion weights come from a separate,
community-maintained Hugging Face repo (`tsolful/Minimax_H3_INT4MixedConvRot`,
see `H3_HF_REPO_INT4` in `config.env`) — `pruned`, `pruned_scaled`,
`balanced`, and `max` all come from the official `Comfy-Org/MiniMax-H3` repo.
The installer accepts each model's license independently, so installing
`light` after already accepting an official-repo tier's license (or vice
versa) may prompt for a second license acceptance the first time. INT4Q was
chosen over the smaller/faster INT4BQ variant specifically for visual
quality (INT4Q keeps ~73-75% of layers at INT8 precision vs ~39-47% for
INT4BQ) — see `lib/models.sh` for the full reasoning.

`pruned` (INT8 ConvRot) is what the upstream Comfy-Org README itself
recommends — its own words: *"prefer int8_convrot if you are able to use
pytorch with cu130; fp8_scaled should only be used if you for any reason
can not use the int8_convrot."* `pruned_scaled` exists for exactly that
fallback case and is never picked by `--tier=auto` (see below) — it's a
deliberate manual choice only.

\* One diffusion model (t2v/i2v share FL2VA; r2v uses REF2VA) + text encoder
+ both VAEs (~3 GB, tier-independent). Installing all workflows downloads
both FL2VA and REF2VA, roughly doubling the diffusion-model portion. Exact
figures live in `lib/models.sh` and are what `install.sh` uses for its own
disk-space check — this table is for planning, not authoritative.

**The default is `auto`** (set in `config.env`) — it detects VRAM and picks
the heaviest tier that fits, with a safety margin
(`H3_TIER_VRAM_SAFETY_MARGIN_GB`). `pruned_scaled` is never selected by
`auto` regardless of VRAM — only `light`, `pruned`, `balanced`, or `max`. To
force a tier explicitly:

```bash
bash install.sh --tier=light
bash install.sh --tier=pruned
bash install.sh --tier=pruned_scaled
bash install.sh --tier=balanced
bash install.sh --tier=max
```

(`bash wizard.sh` asks for this interactively instead — see
[above](#-the-wizard-in-detail).)

> **Compatibility note:** the official workflow JSON files reference the
> pruned FP8 scaled filenames in their model-loader nodes — now the
> `pruned_scaled`-tier filenames specifically (not `balanced`'s anymore,
> since `balanced` moved to pruned BF16). Regardless of which tier you
> actually install, if it isn't `pruned_scaled` you'll need to reselect the
> correct file once in each workflow's loader node the first time you open
> it. See
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#a-workflow-says-a-model-is-missing-even-though-checksh-says-its-installed).
>
> **Turbo LoRA note:** the bundled Turbo LoRA
> (`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`) was converted and validated
> specifically against the pruned/curve-form (int8_convrot) checkpoint —
> i.e. the `pruned` tier — where it loads natively. On `pruned_scaled`
> (fp8_scaled) or `balanced` (BF16) it is **not** verified; see
> `config.env` (`MINIMAX_H3_TURBO_LORA_URL`) for the manual fallback if
> `MiniMaxH3TurboLoRA` fails to load.

---

## 🎬 Workflows

`install.sh --workflows=` (or `H3_WORKFLOWS` in `config.env`) selects which
video tasks to install for: `t2v`, `i2v`, `r2v`, any comma-separated
combination, or `all` (default). Only the diffusion models required by your
selection are downloaded, and only workflow files whose required models are
all present get copied into ComfyUI.

All 5 official workflow files under `workflows/` are eligible:

|File|Task|
|-|-|
|`video_minimax_h3_t2v.json`|Text → Video|
|`video_minimax_h3_i2v.json`|Image → Video|
|`video_minimax_h3_r2v.json`|Reference → Video|
|`minimaxH3T2VI2VREF2VAdvanced_v15.json`|Combined advanced graph (all three)|
|`MiniMaxH3_AllInOne.json`|All three tasks in one graph|

They appear in ComfyUI under **Workflows → Browse Templates** (or directly
in `ComfyUI/user/default/workflows/`) after install — no manual import.

---

## 🧩 Presets (extra models for a specific workflow)

Presets bundle a fixed set of models — and their matching workflow, and any
custom node it needs — for one specific community workflow. By default they
add on top of the standard install (`--tier`/`--workflows` still apply);
`H3_PRESETS=""` (or `--preset=` with no value) reproduces the exact
behavior this project had before this feature existed (no preset, standard
tier only).

**As of 2026-08, `dasiwa_mmh3v12` is the project's default** (`H3_PRESETS`
in `config.env`) — a deliberate choice, not a fallback. Unlike the other
presets, it's listed in `H3_PRESET_REPLACES_STANDARD_TIER`: since its own
FL2VA/REF2VA/text-encoder checkpoints cover the same role as the standard
`H3_TIER` weights (just a different precision/repo), downloading both would
waste ~40–80 GB of redundant weights for no benefit — so `install.sh` skips
the standard-tier download entirely whenever a preset from that list is
active. Set `H3_PRESETS=""` in `config.env` (or `--preset=` on the command
line) to opt back into the standard `H3_TIER` weights instead.

```bash
bash install.sh --preset=aistudynow                # full install + this preset
bash install.sh --only-models --preset=aistudynow   # (re)download just this preset's models
bash install.sh --preset=                           # disable the default preset, use the standard H3_TIER tier
```

Or set it permanently in `config.env`:

```bash
H3_PRESETS="aistudynow"
```

Multiple presets: `--preset=aistudynow,other_preset`. An unknown preset name
is ignored with a warning, never a hard failure.

|Preset|What it installs|
|-|-|
|`aistudynow`|Experimental W4A8 MiniMax H3 Reference-to-Video checkpoint (Kijai/MiniMax-H3-experimental), its matching INT8 ConvRot video VAE and rank-256 reference LoRA, plus the NVFP4 AWQ text encoder and audio VAE already used by the standard install (skipped if already present) — and the dedicated `MiniMax_H3_REF2V_AIStudyNow.json` workflow.|
|`dasiwa_mmh3v12` **(default)**|"DaSiWa - MiniMaxH3 MythicAlchemy v12" (T2VA/I2VA/FLF2VA/REF2VA) checkpoints — INT8 ConvRot FL2VA + REF2VA (Comfy-Org/MiniMax-H3, outside the standard tiers) and the INT4 ConvRot text encoder (Abiray/MiniMax-H3-GGUF) as selected in the workflow's Settings node — plus the fp16 video VAE and fp32 audio VAE (already used by the standard install, repeated here so the preset is self-contained), the TAE fast-preview model (Kijai/MiniMax-H3-TAE), the AnimeSharpV4 upscale model (Kim2091/2x-AnimeSharpV4), the RIFE 4.26 frame-interpolation model (Comfy-Org/frame_interpolation), and the `ComfyUI-DaSiWa-Nodes` custom node (required by the workflow's LoRA loader and Director nodes). 4 diffusion/VAE files also get symlinked into a `MiniMaxH3/` subfolder under `diffusion_models/`/`vae/`, matching the node pack's own naming convention. **Replaces the standard `H3_TIER` download** (see above) — INT8 ConvRot was kept deliberately over FP8 scaled: community benchmarks and the upstream Comfy-Org README both favor INT8 ConvRot on Ampere-class cards.|
|`minimaxh3auto_v5`|"Minimax H3 Auto-Prompter" (T2VA/I2VA/L2VA/FL2VA/REF2V) — downloads the local GGUF prompt-writing LLM (DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF) and its vision projector `mmproj-F16.gguf` into `models/LLM`, plus the `Qwen3.5-9B-abliterated` CLIP text encoder (lukey03/Qwen3.5-9B-abliterated) into `models/text_encoders`, and the dedicated `MinimaxH3Auto_v5.json` workflow. **Filename caveat**: the repo file is `model.safetensors` — this project's preset downloader never renames files, but the workflow's CLIPLoader widget expects `Qwen3.5-9B-abliterated.safetensors`, so rename it once after download (`mv ComfyUI/models/text_encoders/model.safetensors ComfyUI/models/text_encoders/Qwen3.5-9B-abliterated.safetensors`) or reselect `model.safetensors` in the node's dropdown. **Requires the `ComfyUI-LLM-text-processor` custom node** (installed separately via `OPTIONAL_NODE_REPOS`, not by the preset itself). Its llama.cpp backend only ships official Windows x64+CUDA 13 binaries — on this Linux/RunPod install it needs a Linux-built llama.cpp binary in place manually before the node will run (see the node's [README](https://github.com/KingManiya/ComfyUI-LLM-text-processor#llamacpp)); this installer does not build or fetch one for you.|

Adding a new preset later only means: a manifest entry in `config.env`
(`PRESET_<NAME>`, `H3_PRESET_NAMES`, optionally `H3_PRESET_WORKFLOWS`,
`PRESET_<NAME>_NODE_REPOS`, `PRESET_<NAME>_SYMLINKS`, and
`H3_PRESET_REPLACES_STANDARD_TIER` if it should replace rather than
supplement the standard tier) and a workflow file under `presets/<name>/` —
nothing in `lib/presets.sh` needs to change.

### Installing only ComfyUI/CUDA/PyTorch + a preset's own models

If you only want a specific preset's models — not the standard tier's —
combine `--skip-models` (skips the standard `H3_TIER` weights) with
`--preset=` (presets are additive and independent of `--skip-models`, so
they're still honored):

```bash
bash install.sh --skip-models --preset=dasiwa_mmh3v12
```

This installs system packages, GPU/CUDA setup, PyTorch, ComfyUI,
ComfyUI-Manager, the custom nodes — everything except the standard tier's
weights — then downloads only the preset's models and copies its workflow
in. Note this also skips the standard turbo LoRA install
(`install_turbo_node`/`install_turbo_lora` only run when standard models
aren't skipped); install it manually later via `bash install_lora.sh` if a
given workflow needs it.

---

## 📦 Installed components

* ComfyUI (default branch, so native MiniMax H3 support is always current)
* ComfyUI-Manager
* ComfyUI-VideoHelperSuite
* Spectrum MiniMax H3 (optional acceleration node, see below)
* PyTorch + torchvision + torchaudio, matched to your detected CUDA runtime
* Hugging Face CLI, `hf_xet`
* System packages: git, git-lfs, wget, curl, aria2, ffmpeg, tmux, unzip,
  Python 3 + venv/pip, build-essential

---

## 🔒 Pinning a ComfyUI commit (reproducible installs)

By default (`COMFYUI_COMMIT` empty in `config.env`), a fresh install clones
whatever the latest commit of `COMFYUI_BRANCH` (`master`) happens to be at
install time — this keeps native MiniMax H3 support current, but it also
means two installs on different days can end up on different ComfyUI code,
including its `comfy-kitchen`/`comfy-aimdo` pins.

To reproduce a specific, known-good state (e.g. to bisect a regression, or
to freeze a validated install), set `COMFYUI_COMMIT` in `config.env`:

```bash
COMFYUI_COMMIT="a1b2c3d..."
```

`install.sh` and `update.sh` will then check out that exact commit right
after cloning/updating ComfyUI, regardless of where `COMFYUI_BRANCH` itself
is currently pointing. Leave it empty to keep tracking the branch as before.
It can also be set inline for a single run:

```bash
COMFYUI_COMMIT=e1e4413 bash bootstrap.sh
```

---

## 🎨 Installing and managing LoRAs

```bash
bash install_lora.sh "https://..."                 # install (skips if already present)
bash install_lora.sh --force "https://..."          # reinstall
bash install_lora.sh --list                         # list installed LoRAs with sizes
bash install_lora.sh --remove some.safetensors
```

Hugging Face, CivitAI (`civitai.com` / `civitai.red`), and any direct
`.safetensors` URL are supported. No authentication is needed for public
files; set `CIVITAI_API_KEY` for restricted CivitAI content. LoRAs land in
`ComfyUI/models/loras/`. See [RECOMMENDED_LORAS.md](RECOMMENDED_LORAS.md)
for a tested example (MiniMax H3 Turbo, 4-step sampling).

Run these from inside the project directory — if unsure where that is:

```bash
cd /workspace/minimax-runpod-installer-*
```

---

## ⚡ Spectrum MiniMax H3 (optional)

[Spectrum MiniMax H3](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3)
is an optional acceleration node for MiniMax H3.

* Enabled by default, no additional Python dependencies
* Installs automatically into `custom_nodes/`
* Toggle it interactively via `bash wizard.sh`, or disable it permanently in
  `config.env`:

```bash
INSTALL_SPECTRUM=false
```

---

## 🖇 Surviving web-terminal disconnects (tmux)

RunPod's web terminal can disconnect while a long task (model download,
install, generation) is still running. Both `bash wizard.sh` and
`bash bootstrap.sh` protect you from this automatically: ComfyUI ends up
running inside a persistent `minimax` tmux session, so a dropped connection
never kills the process.

To reattach after a disconnect (or from a new terminal):

```bash
bash menu.sh   # then choose option 6
# or directly:
bash launch.sh --tmux
```

If that prints `sessions should be nested with care`, you're already inside
a tmux client (common on RunPod's web terminal) — use
`tmux switch-client -t minimax` instead of trying to attach again.

Full details, including how to protect `install.sh` itself and how to
manage the session manually, are in [TMUX.md](TMUX.md).

---

## 🖥 CLI reference

```bash
bash wizard.sh                        # guided setup — recommended starting point

bash install.sh                       # full install, defaults from config.env
bash install.sh --skip-models         # install everything except H3 weights
bash install.sh --only-models         # (re)download weights only
bash install.sh --tier=light          # force a weight tier (light/pruned/pruned_scaled/balanced/max)
bash install.sh --tier=auto           # pick a tier from detected VRAM
bash install.sh --workflows=t2v,r2v   # only install these workflows' models
bash install.sh --preset=aistudynow   # + this preset's models/workflow (additive)
bash install.sh --preset=             # disable the default preset, use the standard H3_TIER tier
bash install.sh --skip-models --preset=dasiwa_mmh3v12  # ComfyUI/CUDA/PyTorch only + this preset's models
bash install.sh --yes                 # non-interactive, answers "yes" everywhere
bash install.sh --force               # redo every step, ignore prior state

bash update.sh                        # update ComfyUI, nodes, PyTorch, deps
bash check.sh                         # verify installation, no changes made
bash launch.sh                        # start ComfyUI in the foreground
bash launch.sh --tmux                 # start/reattach inside tmux
bash launch.sh --stop                 # stop a running ComfyUI process
bash menu.sh                          # interactive menu wrapping all of the above
bash uninstall.sh                     # remove ComfyUI (optionally keep models)
bash install_lora.sh <URL>            # install/list/remove LoRAs, see above
```

---

## 🖥 GPU support

Any NVIDIA GPU with **8 GB+ VRAM** (`MIN_VRAM_GB` in `config.env`) works, at
the tier that fits it — see [Model tiers](#-model-tiers-h3_tier).

|VRAM|Tier|Example GPUs|
|-|-|-|
|48 GB+|`max`|RTX A6000, RTX 6000 Ada, L40S, A100 80GB, H100, H200|
|40–47 GB|`balanced`|RTX A6000 (light load), L40, A100 40GB|
|24–39 GB|`pruned` (auto) / `pruned_scaled` (manual fallback)|RTX 4090, RTX 3090, A40, L4|
|8–23 GB|`light`|RTX 3060 and similar|

GPUs outside this "known" list still work as long as VRAM is sufficient —
the installer only warns, it doesn't block on an unrecognized card.

---

## 📁 Project structure

```
.
├── wizard.sh              # interactive setup wizard — recommended entry point
├── bootstrap.sh           # one-command, non-interactive entry point: clone/update, install, launch in tmux
├── install.sh             # full installer (see CLI reference)
├── update.sh              # update ComfyUI/nodes/PyTorch without touching models
├── check.sh               # read-only verification
├── launch.sh              # start ComfyUI (optionally in tmux)
├── menu.sh                # interactive menu wrapping the scripts above
├── uninstall.sh           # remove ComfyUI (optionally keep models/)
├── install_lora.sh        # standalone LoRA install/list/remove
├── config.env             # central configuration (paths, tiers, sources, ...)
├── requirements.txt       # project-level Python deps (on top of ComfyUI's own)
├── lib/
│   ├── utils.sh           # logging, error handling, step tracking, retries
│   ├── system.sh          # apt package installation
│   ├── gpu.sh              # GPU/VRAM/CUDA detection, tier recommendation
│   ├── python.sh           # venv, PyTorch build selection & install, CUDA checks
│   ├── comfyui.sh          # clone/update the ComfyUI repo itself
│   ├── manager.sh          # ComfyUI-Manager install/update
│   ├── nodes.sh             # optional custom nodes (VideoHelperSuite, Spectrum, ...)
│   ├── huggingface.sh       # HF auth + gated-repo access check
│   ├── download.sh          # generic HF file download (hf-cli, resume, verify)
│   ├── models.sh            # H3 tier/workflow resolution, manifest, download orchestration
│   ├── workflows.sh         # copies workflow JSON matching the current selection, patches filenames per tier
│   ├── presets.sh            # extra per-workflow model sets, nodes, symlinks (see Presets above)
│   ├── optimization.sh      # GPU-tuned ComfyUI launch flags
│   └── verify.sh            # check.sh backend + install summary
├── workflows/               # official MiniMax H3 workflow JSON files (see Workflows above)
├── presets/                 # preset-specific workflow JSON files (see Presets above)
│   ├── aistudynow/
│   ├── dasiwa_mmh3v12/
│   └── minimaxh3auto_v5/
├── docs/
│   ├── INSTALL_EN.md        # detailed step-by-step guide (English)
│   └── INSTALL_FR.md        # detailed step-by-step guide (French)
├── TMUX.md
├── FAQ.md
├── TROUBLESHOOTING.md
├── RECOMMENDED_LORAS.md
└── CHANGELOG.md
```

---

## 📚 Documentation

* [Installation Guide — English](docs/INSTALL_EN.md)
* [Guide d'installation — Français](docs/INSTALL_FR.md)
* [FAQ](FAQ.md)
* [Troubleshooting](TROUBLESHOOTING.md)
* [Using tmux with this project](TMUX.md)
* [Recommended LoRAs](RECOMMENDED_LORAS.md)
* [Changelog](CHANGELOG.md)

---

## 🛣 Roadmap

* Screenshots of the install flow and generated output
* Backup & restore helper for `models/`
* Plugin system for optional custom nodes beyond `config.env`'s static list

---

## 🤝 Contributing

Pull requests are welcome. If you find a bug or have a feature request,
please open an issue.

---

## 📜 License

Apache License 2.0

---

## ⭐ Support the project

If this project saved you time, please consider giving it a ⭐ on GitHub.
