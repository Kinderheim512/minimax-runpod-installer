# Changelog

All notable changes to this project will be documented in this file.

This project follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

Changes since `v1.1.0`, not yet tagged.

### 🧠 `H3_TIER` split from 3 to 5 tiers; `wizard.sh` model-tier picker; workflow auto-patch bug fixed

- The official `Comfy-Org/MiniMax-H3` repo added `pruned_bf16` (~40.2 GB,
  full precision, no lossy quantization) alongside the already-known
  `pruned_int8_convrot`/`pruned_fp8_scaled` (~21 GB each). Rather than keep
  `balanced` on `fp8_scaled` (documented upstream as a fallback, not a
  recommendation), the tier is split into five explicit choices:
  `light` (unchanged, INT4Q, third-party repo), `pruned` (**new** —
  INT8 ConvRot, the upstream-recommended choice, restores native Turbo LoRA
  compatibility), `pruned_scaled` (**new** — FP8 scaled, explicit manual
  fallback, never auto-selected), `balanced` (**redefined** — now
  `pruned_bf16`, i.e. "the official models" at full precision), `max`
  (unchanged, BF16 unpruned). Every tier except `light` now comes from the
  official repo.
- `lib/gpu.sh::detect_gpu` — VRAM auto-detection ladder gained a `pruned`
  rung (new `H3_TIER_MIN_VRAM_PRUNED_GB`, default 24) between `light` and
  `balanced`; `H3_TIER_MIN_VRAM_BALANCED_GB` default raised 24→40 to match
  `balanced`'s new, bigger checkpoint. `pruned_scaled` is deliberately
  absent from this ladder — always a manual `--tier=` choice, never picked
  by `auto`.
- `wizard.sh`'s tier question now lists all 5 tiers with their approximate
  size/VRAM instead of 3.
- Fixed a latent bug in `lib/workflows.sh::_known_filenames_for_key()`: the
  manifest format is `"repo|subpath|tier|size"` (4 fields, since the
  multi-repo architecture change), but the field-extraction line was never
  updated and still read only 3 — `IFS='|' read -r subpath _ _` silently
  captured the **repo** name (e.g. `MiniMax-H3`) into `$subpath` instead of
  the actual filename. This broke `_patch_workflow_tier_filenames()`'s
  candidate matching for every tier, always, since that field was added —
  found while verifying the 5-tier change end-to-end, unrelated to it
  otherwise. Fixed to `read -r _ subpath _ _`.

### 🔀 `dasiwa_mmh3v12` becomes the default preset; symlink-install crash fixed

- `H3_PRESETS` now defaults to `"dasiwa_mmh3v12"` (`config.env`) — a
  deliberate project choice, not a fallback. `H3_PRESETS=""` (env) or
  `--preset=` (empty value) opts back into no preset / the standard
  `H3_TIER` weights. Implemented with `${H3_PRESETS-default}` (no `:`),
  not `${H3_PRESETS:-default}`, so an explicitly empty value is honored
  rather than silently replaced by the default — the two expansions differ
  precisely on that case.
- New `H3_PRESET_REPLACES_STANDARD_TIER` array (`config.env`) — presets
  listed here provide their own complete fl2va/ref2va/text-encoder set
  (under a different precision/repo), so `install.sh` now skips
  `download_h3_models()` entirely when one is active, avoiding ~40–80 GB of
  redundant duplicate weights. Only `dasiwa_mmh3v12` is listed; `aistudynow`
  and `minimaxh3auto_v5` remain purely additive. New
  `preset_replaces_standard_tier()` helper in `lib/presets.sh`; both
  `install.sh` code paths (`--only-models` and the full install) now
  resolve `H3_ACTIVE_PRESETS` once, before deciding whether to call
  `download_h3_models()`, instead of resolving it twice as before.
- Kept `dasiwa_mmh3v12`'s own manifest on **INT8 ConvRot** (not FP8 scaled)
  after reviewing community benchmarks: SSIM fidelity and generation speed
  both favor INT8 ConvRot over FP8 scaled on Ampere-class cards, consistent
  with the upstream Comfy-Org README's own guidance (FP8 scaled documented
  as a fallback for when INT8 ConvRot can't be used, not an upgrade).
- Fixed a crash in `install_preset_symlinks()` (`lib/presets.sh`): when
  called as a bare statement under `set -Eeuo pipefail` (as `install.sh`
  does), the function fell through to `[[ "$any_declared" == "false" ]] &&
  return 0` as its *last* statement — which evaluates to exit status 1
  whenever a preset's symlinks *were* declared and successfully created,
  aborting the whole install immediately after logging success for every
  individual link. Fixed with an explicit `if/fi` that always ends on a
  real `return 0`. Hardened `install_preset_nodes()` (same file) against
  the identical pattern for consistency, even though it wasn't yet
  observed to fail there (it happened to always end on `log_ok`, which
  currently returns 0).
- New `symlinks-fix` verified end-to-end with `set -e` re-execution
  (including idempotent re-run) against a mocked `INSTALL_DIR`, not just
  `bash -n`.

### 🧩 Presets — extra models for a specific workflow

- New `--preset=<name>` flag (`H3_PRESETS` in `config.env`, comma-separated
  for several presets at once) downloads a fixed set of model files and
  installs the matching workflow, **on top of** the standard `--tier`/
  `--workflows` install by default — never altering it. Leaving it unset
  (before 2026-08) reproduced the exact prior behavior; see "`dasiwa_mmh3v12`
  becomes the default preset" above for the current default.
- First preset: `aistudynow` — the experimental W4A8 MiniMax H3
  Reference-to-Video checkpoint (`Kijai/MiniMax-H3-experimental`), its
  matching INT8 ConvRot video VAE and rank-256 reference LoRA, plus the
  NVFP4 AWQ text encoder and audio VAE already used by the standard install
  (skipped if already present — `download_hf_file()` is idempotent), and a
  dedicated `MiniMax_H3_REF2V_AIStudyNow.json` workflow.
- New `lib/presets.sh` module and `presets/` directory. Deliberately kept
  separate from `lib/workflows.sh`/`workflows/`: that path's per-tier
  filename rewriting (`_patch_workflow_tier_filenames`) must never touch a
  preset's fixed model filenames.
- Adding a future preset needs no code change: a manifest entry in
  `config.env` (`PRESET_<NAME>`, `H3_PRESET_NAMES`, optionally
  `H3_PRESET_WORKFLOWS`) and a workflow file under `presets/<name>/`.

### 🧬 H3 weight tiers modernized, multi-repo model architecture

- **`light`** now uses the **INT4Q** mixed INT4/INT8 ConvRot quantization
  (`tsolful/Minimax_H3_INT4MixedConvRot` on Hugging Face) instead of the
  previous pruned INT8 ConvRot weights. INT4Q was chosen over the smaller
  INT4BQ variant on quality grounds: INT4Q keeps ~73-75% of layers at INT8
  precision (vs ~39-47% for INT4BQ), for a size difference of only ~2.6 GB —
  not enough to justify the larger quality gap for a project that prioritizes
  visual fidelity over disk footprint. `light` remains meaningfully smaller
  than `balanced` (~18.5 GB vs ~21 GB diffusion weights per checkpoint).
- **`balanced`** now uses the **pruned FP8 scaled** diffusion weights
  (~21 GB per checkpoint; previously pruned INT8 ConvRot, itself previously
  `light`'s weights, replacing the older, larger non-pruned INT8 ConvRot
  weights, ~34 GB) and the **NVFP4 AWQ** text encoder (~15.7 GB —
  previously `light`-only, replaces the older INT8 ConvRot text encoder,
  ~27.1 GB, no longer referenced by any tier). The FP8-scaled switch was a
  deliberate choice by the project maintainer, made aware that the
  Comfy-Org README documents FP8 scaled as a fallback for int8_convrot
  ("use only if you can't use int8_convrot"), not an upgrade, and that the
  bundled Turbo LoRA (`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`) was validated
  specifically against the pruned/curve-form (int8_convrot) checkpoint —
  its compatibility with fp8_scaled is unverified. See `config.env`
  (`MINIMAX_H3_TURBO_LORA_URL`) and `lib/models.sh` for the manual fallback
  if `MiniMaxH3TurboLoRA` fails to load.
- **`max`** is unchanged (BF16).
- Two accelerators evaluated and deliberately **not** integrated: AsymW4A8
  (depends on an unmerged comfy-kitchen PR and mandatory custom nodes — goes
  against this project's "official tools only" philosophy) and BlockCache
  (Spectrum / EasyCache / F1B0 — the community hasn't converged on one yet;
  revisit later).
- **Model manifest is now multi-repo**: each entry in
  `H3_DIFFUSION_FL2VA`/`H3_DIFFUSION_REF2VA`/`H3_TEXT_ENCODER`/`H3_VAE`
  (`lib/models.sh`) now carries its own Hugging Face repo instead of assuming
  a single global `H3_HF_REPO`. `build_h3_model_manifest()` populates a new
  `H3_MODEL_REPO[key]` alongside the existing `H3_MODEL_FILES[key]`. Adding a
  future third-party repo needs only a new manifest entry, no code changes
  elsewhere. New `H3_HF_REPO_INT4` in `config.env` (default
  `tsolful/Minimax_H3_INT4MixedConvRot`) holds the `light`-tier repo.
- **License/gated-access check is now generic**: `hf_check_h3_access()`
  (hardcoded to a single repo) is replaced by `hf_check_repo_access(repo)` +
  `hf_check_required_access(repo1 [repo2 ...])` (`lib/huggingface.sh`), and
  new `h3_required_repos()` (`lib/models.sh`) derives exactly which repos
  need checking from the models actually missing *and* required by the
  current tier/workflow selection — so a `light`-tier install checks both
  Hugging Face repos, while `balanced`/`max` only ever check one.
- **`MODEL_SOURCE=civitai` disabled entirely**: CivitAI only ever hosted the
  pruned INT8 ConvRot weights. It was briefly guarded to `--tier=balanced`
  only (since that used to be `balanced`'s diffusion weights), but now that
  `balanced` itself has moved to pruned FP8 scaled, no tier matches the
  CivitAI files anymore — selecting `MODEL_SOURCE=civitai` now fails fast
  with a clear error instead of silently downloading an int8_convrot file
  under an fp8_scaled filename. `H3_CIVITAI_FL2VA_URL`/
  `H3_CIVITAI_REF2VA_URL` (`config.env`) still exist for manual use.
- New optional custom nodes installed by default: `rgthree-comfy` and
  `ComfyUI-KJNodes` (both have a `requirements.txt`, installed like
  `ComfyUI-VideoHelperSuite`), and `ComfyUI-SolAttn_triton` (no Python
  dependencies of its own — relies on the project's existing PyTorch/Triton
  install, same as Spectrum).

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

### 🧊 VRAM stability on 48 GB cards (`--disable-smart-memory`)

- `--disable-smart-memory` is now part of the default launch flags on pods
  with ample host RAM, via the new `COMFY_SMART_MEMORY` (`auto`/`true`/
  `false`, `config.env`, default `auto`). ComfyUI's speculative VRAM cache
  ("smart memory") was leaving too little headroom on 48 GB cards (RTX
  A6000, RTX 6000 Ada...) running H3, causing OOMs that were not reliably
  reproducible — the same generation could succeed once and fail the next
  time at identical settings, in either the sampler or the VAE decode step.
  This is an **empirical choice, not a general ComfyUI memory-management
  rule**: on the tests run for this project (RTX A6000, 48 GB, RunPod,
  500+ GB host RAM), it eliminated these OOMs up to 15 s / 2.0 MP, against
  frequent, irreproducible failures without it — validated specifically
  for the MiniMax H3 pipeline, not for ComfyUI workflows/models in general.
  It's a `main.py` launch flag, so it applies to the whole ComfyUI process,
  not just H3 — if you load other models/workflows in the same instance,
  this flag applies to them too, untested by us for those cases. Re-check
  if a future ComfyUI release changes its memory-management behavior.
  `auto` only enables it on pods that already meet
  `H3_MIN_RAM_FOR_SMART_MEMORY_GB` (80 GB by default — a dedicated
  threshold, same default value as `COMFY_PINNED_MEMORY`'s today but
  independent, since the two guard against different failure modes and may
  need to diverge) — **this flag is not universally beneficial**: on a
  host where RAM itself is the constrained resource (well under 32 GB), the
  extra forced offload to RAM can make things worse instead of better (see
  `TROUBLESHOOTING.md`). Set `COMFY_SMART_MEMORY=false` to disable outright,
  e.g. once a future ComfyUI/`comfy-kitchen` release makes this workaround
  unnecessary.

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
