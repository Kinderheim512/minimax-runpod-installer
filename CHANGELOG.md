# Changelog

All notable changes to this project will be documented in this file.

This project follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

Changes since `v1.1.0`, not yet tagged.

### 🧠 H3 tiers and workflow-aware installation

- Three weight tiers per GPU class — `light` (8 GB+), `balanced` (24 GB+),
  `max` (48 GB+, default) — selectable via `H3_TIER` in `config.env` or
  `install.sh --tier=`. `--tier=auto` picks a tier from detected VRAM
  instead of using the fixed default.
- `H3_WORKFLOWS` / `install.sh --workflows=t2v,i2v,r2v` (or `all`) selects
  which of the three video tasks to install for — only the diffusion models
  and workflow files actually required by the selection are downloaded or
  copied.
- Dynamic disk-space estimation before any model download starts, based on
  the models actually missing *and* actually required by the current tier
  and workflow selection (replaces the previous fixed threshold).
- Workflow installation is now workflow-aware: a workflow file is only
  copied into ComfyUI if every model it references is required by the
  current `H3_WORKFLOWS` selection.

### ⚡ PyTorch / CUDA

- PyTorch build (version + CUDA index) is now auto-detected from the CUDA
  runtime reported by `nvidia-smi` via a single lookup table
  (`PYTORCH_BUILD_TABLE` in `lib/python.sh`), instead of a version pinned
  in code. `TORCH_VERSION_OVERRIDE` / `TORCH_CUDA_INDEX_OVERRIDE` force a
  specific build when needed.
- PyTorch is installed exactly once, before ComfyUI's own
  `requirements.txt`, with a post-install check that CUDA is still
  available afterwards.

### 🖇 tmux

- `bootstrap.sh` now launches ComfyUI inside a persistent `minimax` tmux
  session automatically (`launch.sh --tmux`), so a fresh install survives a
  RunPod web-terminal disconnect without any extra steps.
- `launch.sh --tmux` creates the session if it doesn't exist, or reattaches
  to it if it does; it also detects a non-interactive shell and prints
  reattach instructions instead of failing.
- `menu.sh` gained a dedicated "Launch ComfyUI (tmux recommended)" option.

### 🎨 LoRA manager

- `install_lora.sh --list` and `--remove <file>`, in addition to the
  existing install-by-URL usage.
- Optional `CIVITAI_API_KEY` support for restricted CivitAI downloads.
- Downloaded files are checked to actually be `.safetensors` (not an HTML
  error/login page saved under that name) before being accepted.

### 📥 Downloads

- Hugging Face downloads now go exclusively through `hf download` /
  `huggingface-cli download` (native Xet protocol). The aria2c path and its
  fallback-to-`hf`-on-failure have been removed: aria2c only ever followed
  the `resolve/main/...` redirect to Hugging Face's legacy LFS bridge, a
  single presigned URL with a short (~1h) expiry — usually fine, but prone
  to random `403`s near the end of very large (tens of GB) downloads, with
  a confusing mid-transfer switch to a second progress bar. `hf download`
  avoids this entirely: the Xet protocol fetches large files as many
  short-lived presigned URLs (one per ~64 MB block), requested throughout
  the transfer instead of once upfront.
- `USE_ARIA2` / `ARIA2_CONNECTIONS` removed from `config.env` (no longer
  used). `aria2c` remains installed at the system level
  (`lib/system.sh`) but is no longer wired into any download path.
  CivitAI, LoRA, and other direct-URL downloads are unaffected — they
  already used `curl` exclusively.
- `HF_HUB_ENABLE_HF_TRANSFER` and the `hf_transfer` dependency removed
  (`config.env`, `lib/download.sh`, `lib/optimization.sh`,
  `requirements.txt`): deprecated and a silent no-op now that `hf_xet` is
  installed by default (`huggingface_hub>=0.32.0`) — all transfers already
  go through the Xet protocol, governed by `HF_XET_HIGH_PERFORMANCE`.
- Known upstream limitation documented in `TROUBLESHOOTING.md`: `hf
  download` resume on very large interrupted files is currently unreliable
  (open `huggingface_hub`/`xet-core` bugs, not specific to this
  installer).

### 🔒 Reproducibility

- Optional `COMFYUI_COMMIT` in `config.env`: when set, `install.sh` /
  `update.sh` check out that exact ComfyUI commit right after cloning or
  updating, instead of always tracking the latest commit of
  `COMFYUI_BRANCH`. Empty by default — no change to existing behaviour.
  Useful to bisect an upstream regression or freeze a validated install.

### 🛠 Reliability / maintenance

- Optional SHA256 verification for MiniMax H3 models (`MODEL_SHA256` in
  `config.env`, or `H3_VERIFY_SHA256_ONLINE=true` to fetch the expected
  hash from Hugging Face).
- ShellCheck cleanup across the project; CI now fails on any ShellCheck
  warning or above.
- Fixed Hugging Face model destination paths (models were previously
  flattened into `models/` instead of their correct subfolder in some
  cases).

---

## [1.1.0] - 2026

### 🚀 Major Features

- One-command bootstrap installation
- Automatic ComfyUI installation
- Automatic Python virtual environment creation
- Automatic CUDA / PyTorch installation
- Automatic ComfyUI-Manager installation
- Automatic VideoHelperSuite installation
- Optional Spectrum MiniMax H3 acceleration node

### 🤖 MiniMax H3

- Automatic MiniMax H3 installation
- Automatic model selection
- Official FL2VA, REF2VA, Text Encoder, Video VAE, and Audio VAE support

### 📥 Downloads

- Smart model downloader with resume, automatic retry, corrupted-model
  detection, and re-download of missing files only
- Hugging Face support
- CivitAI support for diffusion models

### 🎬 Workflows

- Automatic installation of the official Text-to-Video, Image-to-Video,
  and Reference-to-Video workflows

### ⚡ Performance

- Automatic GPU detection, VRAM-based optimization, automatic CUDA
  configuration, optimized launch parameters

### 🛠 Utilities

- Installation verification (`check.sh`), update script, interactive menu,
  launch script, automatic repair
- LoRA manager (install by URL)

### 📚 Documentation

- New README, English and French installation guides, FAQ, troubleshooting,
  contributing guide

### 🐞 Bug Fixes

- Fixed duplicate PyTorch installation
- Fixed workflow installation
- Fixed model verification
- Fixed Hugging Face destination folders
- Fixed smart download logic
- Improved installation reliability

---

## [1.0.0]

Initial public release.
