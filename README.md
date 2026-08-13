# 🚀 MiniMax H3 RunPod Installer

<p align="center">

**One-command deployment of ComfyUI + MiniMax H3 on RunPod**

Installs ComfyUI, MiniMax H3, official workflows, and dependencies, and
tunes everything for the GPU it detects.

</p>

<p align="center">

!\[Platform](https://img.shields.io/badge/Platform-RunPod-blue)
!\[ComfyUI](https://img.shields.io/badge/ComfyUI-%3E%3D0.30.0-green)
!\[Python](https://img.shields.io/badge/Python-3.10%2B-yellow)
!\[CUDA](https://img.shields.io/badge/CUDA-auto--detected%20(11.8–13.0)-success)
!\[License](https://img.shields.io/github/license/Kinderheim512/minimax-runpod-installer)

</p>

\---

# ✨ Features

* 🚀 One-command installation (`bootstrap.sh`)
* 🎬 Automatic ComfyUI installation, tracking its default branch
* ⚡ Automatic GPU/CUDA detection and matching PyTorch build install
* 🧠 Three MiniMax H3 weight tiers, picked to fit your VRAM (or forced)
* 🎯 Workflow-aware installs — download only what your selected workflows need
* 📥 Smart model downloader: resume, integrity checks, automatic repair
* 📏 Disk-space estimated and checked *before* any download starts
* 📦 Automatic installation of the official MiniMax H3 workflows
* 🎨 LoRA manager: install, list, and remove from the command line
* 🌐 Hugging Face and CivitAI as model sources
* 📈 Automatic GPU-based launch flag tuning (`--highvram`, `--disable-smart-memory`, `--fast`, attention backend, ...)
* 🖇 Automatic persistent tmux session, survives web-terminal disconnects
* 🔄 Idempotent update system — safe to re-run any time
* 📋 Non-destructive verification (`check.sh`)
* 🖥 RunPod-first, but works on any Linux pod with an NVIDIA GPU

\---

# ⚡ Quick Start

Create a fresh RunPod (PyTorch / CUDA / Ubuntu template, NVIDIA GPU, port
`8188` exposed as HTTP), open its terminal, and run:

```bash
cd /workspace
git clone https://github.com/Kinderheim512/minimax-runpod-installer.git
cd minimax-runpod-installer
bash bootstrap.sh
```

This single command:

* detects your GPU and picks a matching PyTorch/CUDA build
* installs ComfyUI, ComfyUI-Manager, and the optional custom nodes
* creates the Python environment
* downloads the MiniMax H3 weight tier that fits your GPU (or `max` if you
haven't set `H3\_TIER` — see [Model tiers](#-model-tiers-h3_tier) below)
* installs the official workflows
* computes GPU-tuned launch flags
* starts ComfyUI **inside a persistent tmux session** (see [TMUX.md](TMUX.md))

No manual configuration is required for a first run. For anything beyond
the default (a specific tier, a subset of workflows, skipping model
downloads, non-interactive runs), see [CLI reference](#-cli-reference)
below, or the full walkthrough in
[docs/INSTALL\_EN.md](docs/INSTALL_EN.md) / [docs/INSTALL\_FR.md](docs/INSTALL_FR.md).

\---

# 🧠 Model tiers (`H3\_TIER`)

MiniMax H3 ships at three quality/size tiers. `install.sh` downloads only
one tier — the one you select — never all three.

|Tier|Min. VRAM|Diffusion model (FL2VA or REF2VA)|Text encoder|Approx. total\*|
|-|-|-|-|-|
|`light`|8 GB|INT4Q mixed INT4/INT8 ConvRot, \~18.5 GB|NVFP4 AWQ, \~15.7 GB|\~37 GB|
|`balanced`|24 GB|pruned FP8 scaled, \~21 GB|NVFP4 AWQ, \~15.7 GB|\~40 GB|
|`max` (default)|48 GB|BF16, \~66.3 GB|BF16, \~51.5 GB|\~121 GB|

`light`'s diffusion weights come from a separate, community-maintained
Hugging Face repo (`tsolful/Minimax\_H3\_INT4MixedConvRot`, see
`H3\_HF\_REPO\_INT4` in `config.env`) rather than the official `Comfy-Org`
repo used by the other two tiers — the installer accepts each model's
license independently, so installing `light` after already accepting the
`balanced`/`max` license (or vice versa) may prompt for a second license
acceptance the first time. INT4Q was chosen over the smaller/faster INT4BQ
variant specifically for visual quality (INT4Q keeps \~73-75% of layers at
INT8 precision vs \~39-47% for INT4BQ) — see `lib/models.sh` for the full
reasoning.

\* One diffusion model (t2v/i2v share FL2VA; r2v uses REF2VA) + text encoder

* both VAEs (\~3 GB, tier-independent). Installing all workflows downloads
both FL2VA and REF2VA, roughly doubling the diffusion-model portion.
Exact figures live in `lib/models.sh` and are what `install.sh` uses for
its own disk-space check — this table is for planning, not authoritative.

**The default is `max`** (set in `config.env`), regardless of the GPU
detected — it does **not** automatically shrink to fit a smaller card.
To pick a tier automatically based on detected VRAM instead:

```bash
bash install.sh --tier=auto
```

Or force one explicitly:

```bash
bash install.sh --tier=light
```

> \*\*Compatibility note:\*\* the official workflow JSON files reference the
> pruned FP8 scaled filenames in their model-loader nodes, which is now
> the `balanced`-tier filenames (it used to be `light`'s, before `light`
> switched to INT4Q, then `balanced` itself moved from pruned INT8 ConvRot
> to pruned FP8 scaled — see \[Model tiers](#-model-tiers-h3\_tier) above),
> and that regardless of which tier you actually install. If you use `light`
> or `max`, you'll need to reselect the correct file once in each workflow's
> loader node the first time you open it. See
> \[TROUBLESHOOTING.md](TROUBLESHOOTING.md#a-workflow-says-a-model-is-missing-even-though-checksh-says-its-installed).
>
> \*\*Turbo LoRA note:\*\* the Comfy-Org README documents pruned FP8 scaled as
> a fallback ("use only if you can't use int8_convrot"), not an upgrade —
> and the bundled Turbo LoRA (`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`) was
> converted and validated specifically against the pruned/curve-form
> (int8_convrot) checkpoint, not fp8_scaled. If `MiniMaxH3TurboLoRA` fails
> to load on `balanced`, see `config.env` (`MINIMAX_H3_TURBO_LORA_URL`) for
> the manual fallback.

\---

# 🎬 Workflows

`install.sh --workflows=` (or `H3\_WORKFLOWS` in `config.env`) selects which
video tasks to install for: `t2v`, `i2v`, `r2v`, any comma-separated
combination, or `all` (default). Only the diffusion models required by your
selection are downloaded, and only workflow files whose required models are
all present get copied into ComfyUI.

All 5 official workflow files under `workflows/` are eligible:

|File|Task|
|-|-|
|`video\_minimax\_h3\_t2v.json`|Text → Video|
|`video\_minimax\_h3\_i2v.json`|Image → Video|
|`video\_minimax\_h3\_r2v.json`|Reference → Video|
|`minimaxH3T2VI2VREF2VAdvanced\_v15.json`|Combined advanced graph (all three)|
|`MiniMaxH3\_AllInOne.json`|All three tasks in one graph|

They appear in ComfyUI under **Workflows → Browse Templates** (or directly
in `ComfyUI/user/default/workflows/`) after install — no manual import.

\---

# 🧩 Presets (extra models for a specific workflow)

Presets add a fixed set of models (and their matching workflow) **on top of**
the standard install — they never change `--tier`/`--workflows`, and leaving
`--preset=` unset reproduces the exact behavior you had before this feature
existed.

```bash
bash install.sh --preset=aistudynow            # full install + this preset
bash install.sh --only-models --preset=aistudynow  # (re)download just this preset's models
```

Or set it permanently in `config.env`:

```bash
H3\_PRESETS="aistudynow"
```

Multiple presets: `--preset=aistudynow,other\_preset`. An unknown preset name
is ignored with a warning, never a hard failure.

|Preset|What it installs|
|-|-|
|`aistudynow`|Experimental W4A8 MiniMax H3 Reference-to-Video checkpoint (Kijai/MiniMax-H3-experimental), its matching INT8 ConvRot video VAE and rank-256 reference LoRA, plus the NVFP4 AWQ text encoder and audio VAE already used by the standard install (skipped if already present) — and the dedicated `MiniMax\_H3\_REF2V\_AIStudyNow.json` workflow.|
|`dasiwa_mmh3v12`|"DaSiWa - MiniMaxH3 MythicAlchemy v12" (T2VA/I2VA/FLF2VA/REF2VA) checkpoints — INT8 ConvRot FL2VA + REF2VA (Comfy-Org/MiniMax-H3, outside the standard tiers) and the INT4 ConvRot text encoder (Abiray/MiniMax-H3-GGUF) as selected in the workflow's Settings node — plus the fp16 video VAE and fp32 audio VAE (already used by the standard install, repeated here so the preset is self-contained), the TAE fast-preview model (Kijai/MiniMax-H3-TAE), the AnimeSharpV4 upscale model (Kim2091/2x-AnimeSharpV4) and the RIFE 4.26 frame-interpolation model (Comfy-Org/frame\_interpolation) — and the dedicated `DaSiWa\_MiniMaxH3\_MythicAlchemy\_v12.json` workflow.|

Adding a new preset later only means: a manifest entry in `config.env`
(`PRESET\_<NAME>`, `H3\_PRESET\_NAMES`, optionally `H3\_PRESET\_WORKFLOWS`) and a
workflow file under `presets/<name>/` — nothing in `lib/presets.sh` needs to
change.

### Installing only ComfyUI/CUDA/PyTorch + a preset's own models

If you only want a specific preset's models — not the standard tier's — combine
`--skip-models` (skips the standard `H3\_TIER` weights) with `--preset=`
(presets are additive and independent of `--skip-models`, so they're still
honored):

```bash
bash install.sh --skip-models --preset=dasiwa_mmh3v12
```

This installs system packages, GPU/CUDA setup, PyTorch, ComfyUI, ComfyUI-Manager,
the custom nodes — everything except the standard tier's weights — then
downloads only the preset's models and copies its workflow in. Note this also
skips the standard turbo LoRA install (`install_turbo_node`/`install_turbo_lora`
only run when standard models aren't skipped); install it manually later via
`bash install_lora.sh` if a given workflow needs it.

\---

# 📦 Installed components

* ComfyUI (default branch, so native MiniMax H3 support is always current)
* ComfyUI-Manager
* ComfyUI-VideoHelperSuite
* Spectrum MiniMax H3 (optional acceleration node, see below)
* PyTorch + torchvision + torchaudio, matched to your detected CUDA runtime
* Hugging Face CLI, `hf\_xet`
* System packages: git, git-lfs, wget, curl, aria2, ffmpeg, tmux, unzip,
Python 3 + venv/pip, build-essential

\---

# 🔒 Pinning a ComfyUI commit (reproducible installs)

By default (`COMFYUI\_COMMIT` empty in `config.env`), a fresh install clones
whatever the latest commit of `COMFYUI\_BRANCH` (`master`) happens to be at
install time — this keeps native MiniMax H3 support current, but it also
means two installs on different days can end up on different ComfyUI code,
including its `comfy-kitchen`/`comfy-aimdo` pins.

To reproduce a specific, known-good state (e.g. to bisect a regression, or
to freeze a validated install), set `COMFYUI\_COMMIT` in `config.env`:

```bash
COMFYUI\_COMMIT="a1b2c3d..."
```

`install.sh` and `update.sh` will then check out that exact commit right
after cloning/updating ComfyUI, regardless of where `COMFYUI\_BRANCH` itself
is currently pointing. Leave it empty to keep tracking the branch as before.

\---

# 🎨 Installing and managing LoRAs

```bash
bash /workspace/minimax-runpod-installer/install\_lora.sh "https://..."        # install (skips if already present)
bash /workspace/minimax-runpod-installer/install\_lora.sh --force "https://..." # reinstall
bash /workspace/minimax-runpod-installer/install\_lora.sh --list                # list installed LoRAs with sizes
bash /workspace/minimax-runpod-installer/install\_lora.sh --remove some.safetensors
```

Hugging Face, CivitAI (`civitai.com` / `civitai.red`), and any direct
`.safetensors` URL are supported. No authentication is needed for public
files; set `CIVITAI\_API\_KEY` for restricted CivitAI content. LoRAs land in
`ComfyUI/models/loras/`. See [RECOMMENDED\_LORAS.md](RECOMMENDED_LORAS.md)
for a tested example (MiniMax H3 Turbo, 4-step sampling).

\---

# ⚡ Spectrum MiniMax H3 (optional)

[Spectrum MiniMax H3](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3)
is an optional acceleration node for MiniMax H3.

* Enabled by default, no additional Python dependencies
* Installs automatically into `custom\_nodes/`
* Disable in `config.env`:

```bash
INSTALL\_SPECTRUM=false
```

\---

# 🖇 Surviving web-terminal disconnects (tmux)

RunPod's web terminal can disconnect while a long task (model download,
install, generation) is still running. `bash bootstrap.sh` protects you
from this automatically: it ends by launching ComfyUI inside a persistent
`minimax` tmux session, so a dropped connection never kills the process.

To reattach after a disconnect (or from a new terminal):

```bash
bash menu.sh   # then choose option 6
# or directly:
bash launch.sh --tmux
```

Full details, including how to protect `install.sh` itself and how to
manage the session manually, are in [TMUX.md](TMUX.md).

\---

# 🖥 CLI reference

```bash
bash install.sh                       # full install, defaults from config.env
bash install.sh --skip-models         # install everything except H3 weights
bash install.sh --only-models         # (re)download weights only
bash install.sh --tier=light          # force a weight tier
bash install.sh --tier=auto           # pick a tier from detected VRAM
bash install.sh --workflows=t2v,r2v   # only install these workflows' models
bash install.sh --preset=aistudynow   # + this preset's models/workflow (additive)
bash install.sh --skip-models --preset=dasiwa_mmh3v12  # ComfyUI/CUDA/PyTorch only + this preset's models
bash install.sh --yes                 # non-interactive, answers "yes" everywhere
bash install.sh --force               # redo every step, ignore prior state

bash update.sh                        # update ComfyUI, nodes, PyTorch, deps
bash check.sh                         # verify installation, no changes made
bash launch.sh                        # start ComfyUI in the foreground
bash launch.sh --tmux                 # start/reattach inside tmux
bash menu.sh                          # interactive menu for all of the above
bash uninstall.sh                     # remove ComfyUI (optionally keep models)
bash install\_lora.sh <URL>            # install/list/remove LoRAs, see above
```

\---

# 🖥 GPU support

Any NVIDIA GPU with **8 GB+ VRAM** (`MIN\_VRAM\_GB` in `config.env`) works,
at the tier that fits it — see [Model tiers](#-model-tiers-h3_tier).

|VRAM|Tier|Example GPUs|
|-|-|-|
|48 GB+|`max`|RTX A6000, RTX 6000 Ada, L40S, A100 80GB, H100, H200|
|24–47 GB|`balanced`|RTX 4090, RTX 3090, A40, L40, L4|
|8–23 GB|`light`|RTX 3060 and similar|

GPUs outside this "known" list still work as long as VRAM is sufficient —
the installer only warns, it doesn't block on an unrecognized card.

\---

# 📁 Project structure

```
.
├── bootstrap.sh          # one-command entry point: clone/update, install, launch in tmux
├── install.sh            # full installer (see CLI reference)
├── update.sh             # update ComfyUI/nodes/PyTorch without touching models
├── check.sh               # read-only verification
├── launch.sh              # start ComfyUI (optionally in tmux)
├── menu.sh                # interactive menu wrapping the scripts above
├── uninstall.sh           # remove ComfyUI (optionally keep models/)
├── install\_lora.sh        # standalone LoRA install/list/remove
├── config.env             # central configuration (paths, tiers, sources, ...)
├── requirements.txt        # project-level Python deps (on top of ComfyUI's own)
├── lib/
│   ├── utils.sh            # logging, error handling, step tracking, retries
│   ├── system.sh           # apt package installation
│   ├── gpu.sh               # GPU/VRAM/CUDA detection, tier recommendation
│   ├── python.sh            # venv, PyTorch build selection \& install, CUDA checks
│   ├── comfyui.sh           # clone/update the ComfyUI repo itself
│   ├── manager.sh           # ComfyUI-Manager install/update
│   ├── nodes.sh              # optional custom nodes (VideoHelperSuite, Spectrum, ...)
│   ├── huggingface.sh        # HF auth + gated-repo access check
│   ├── download.sh           # generic HF file download (hf-cli, resume, verify)
│   ├── models.sh             # H3 tier/workflow resolution, manifest, download orchestration
│   ├── workflows.sh          # copies workflow JSON matching the current selection
│   ├── presets.sh             # extra per-workflow model sets (see Presets above)
│   ├── optimization.sh       # GPU-tuned ComfyUI launch flags
│   └── verify.sh             # check.sh backend + install summary
├── workflows/               # official MiniMax H3 workflow JSON files (see Workflows above)
├── presets/                  # preset-specific workflow JSON files (see Presets above)
│   └── aistudynow/
├── docs/
│   ├── INSTALL\_EN.md         # detailed step-by-step guide (English)
│   └── INSTALL\_FR.md         # detailed step-by-step guide (French)
├── TMUX.md
├── FAQ.md
├── TROUBLESHOOTING.md
├── RECOMMENDED\_LORAS.md
└── CHANGELOG.md
```

\---

# 📚 Documentation

* [Installation Guide — English](docs/INSTALL_EN.md)
* [Guide d'installation — Français](docs/INSTALL_FR.md)
* [FAQ](FAQ.md)
* [Troubleshooting](TROUBLESHOOTING.md)
* [Using tmux with this project](TMUX.md)
* [Recommended LoRAs](RECOMMENDED_LORAS.md)
* [Changelog](CHANGELOG.md)

\---

# 🛣 Roadmap

* Screenshots of the install flow and generated output
* Model selector inside `menu.sh` (currently CLI/`config.env` only)
* Backup \& restore helper for `models/`
* Plugin system for optional custom nodes beyond `config.env`'s static list

\---

# 🤝 Contributing

Pull requests are welcome. If you find a bug or have a feature request,
please open an issue.

\---

# 📜 License

Apache License 2.0

\---

# ⭐ Support the project

If this project saved you time, please consider giving it a ⭐ on GitHub.

