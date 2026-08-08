# Installation Guide

This guide walks through deploying **ComfyUI + MiniMax H3** on a fresh
RunPod, step by step. For the short version, see the main
[README.md](../README.md#-quick-start).

---

# Requirements

Before starting, make sure you have:

- A RunPod account
- A Hugging Face account, with the MiniMax H3 license accepted at
  <https://huggingface.co/Comfy-Org/MiniMax-H3>, and a
  Hugging Face access token with **Read** permission

---

# GPU and tier selection

Any NVIDIA GPU with **8 GB+ VRAM** works. The installer downloads one of
three MiniMax H3 weight tiers, sized to fit different VRAM budgets:

| GPU VRAM | Tier | Notes |
|---|---|---|
| 48 GB+ | `max` | Highest quality (BF16 weights). **This is the project default**, regardless of your actual GPU. |
| 24–47 GB | `balanced` | Pruned INT8 ConvRot weights, roughly a third the size of `max`. |
| 8–23 GB | `light` | INT4Q mixed INT4/INT8 weights (separate Hugging Face repo), smallest and fastest to download. |

Because the default tier (`max`) does not shrink itself to fit a smaller
GPU, decide up front:

- On a 48 GB+ card, the defaults are fine — just run `bootstrap.sh`.
- On a smaller card, either pass `--tier=light` / `--tier=balanced`
  explicitly, or pass `--tier=auto` to have the installer pick a tier from
  detected VRAM automatically (see [Step 3](#step-3--optional-choose-a-tier-or-workflow-subset)
  below).

See [README.md § Model tiers](../README.md#-model-tiers-h3_tier) for exact
file sizes.

---

# Step 1 — Create a RunPod

Create a new pod.

Recommended template:

- PyTorch
- CUDA 12.x (the installer detects and matches whatever CUDA version your
  pod's driver actually reports — see
  [FAQ § Which CUDA and PyTorch build gets installed?](../FAQ.md#which-cuda-and-pytorch-build-gets-installed))
- Ubuntu

Expose port `8188` as HTTP.

---

# Step 2 — Open the terminal and clone the installer

```bash
cd /workspace
git clone https://github.com/Kinderheim512/minimax-runpod-installer.git
cd minimax-runpod-installer
```

---

# Step 3 — (Optional) choose a tier or workflow subset

Skip this step to use the defaults (`max` tier, all three workflows). To
customize, either edit `config.env` or pass flags directly:

```bash
# Auto-pick a tier from detected VRAM instead of the max default
bash bootstrap.sh --tier=auto

# Or force a specific tier
bash bootstrap.sh --tier=light

# Only install for Text-to-Video and Image-to-Video (skips REF2VA entirely)
bash bootstrap.sh --workflows=t2v,i2v
```

`bootstrap.sh` forwards its arguments to `install.sh` on first run. See
[README.md § CLI reference](../README.md#-cli-reference) for the full flag
list.

---

# Step 4 — Run the installer

```bash
bash bootstrap.sh
```

This automatically:

- Detects your GPU and picks a matching PyTorch/CUDA build
- Creates the Python virtual environment
- Installs ComfyUI, ComfyUI-Manager, and optional custom nodes
  (VideoHelperSuite, Spectrum MiniMax H3)
- Creates every required model folder
- Estimates required disk space, then downloads the MiniMax H3 tier and
  workflows you selected (or the defaults)
- Installs the matching official workflows
- Computes GPU-tuned launch flags
- Starts ComfyUI **inside a persistent tmux session** — see
  [TMUX.md](../TMUX.md) for what that means and how to reattach later

No manual configuration is required beyond what you set in Step 3.

---

# Step 5 — Hugging Face authentication

If prompted, enter your Hugging Face access token (or set `HF_TOKEN` as an
environment variable / RunPod secret beforehand to skip the prompt). The
installer verifies you have access to the gated MiniMax H3 repository
before downloading anything, and tells you exactly what to do if the
license hasn't been accepted yet.

---

# Step 6 — Open ComfyUI

Your pod's URL looks like:

```
https://YOUR-POD-ID-8188.proxy.runpod.net
```

---

# Included workflows

The installer installs the official workflow files that match your
`--workflows` selection (all three tasks by default) — see
[README.md § Workflows](../README.md#-workflows) for the full list of 5
files and what each contains. No manual import needed; they appear
directly in ComfyUI.

> If a workflow's model-loader node shows a file that isn't the one you
> downloaded, that's a known tier-naming mismatch in the official workflow
> files, not a missing model — see
> [TROUBLESHOOTING.md](../TROUBLESHOOTING.md#a-workflow-says-a-model-is-missing-even-though-checksh-says-its-installed).

---

# Installing a LoRA

```bash
bash install_lora.sh "https://civitai.red/api/download/models/XXXX?fileId=XXXX"
bash install_lora.sh --list
```

Installed into `ComfyUI/models/loras/`. See
[README.md § Installing and managing LoRAs](../README.md#-installing-and-managing-loras)
and [RECOMMENDED_LORAS.md](../RECOMMENDED_LORAS.md).

---

# Updating

```bash
git pull
bash install.sh
```

Only missing or outdated components are touched. Already-downloaded models
are never re-downloaded.

---

# Verifying the installation

```bash
bash check.sh
```

Checks GPU, CUDA/PyTorch, the MiniMax H3 models required by your current
tier/workflow selection, ComfyUI-Manager, Spectrum, and free disk space —
without changing anything.

---

# Troubleshooting

Covered in a single dedicated page so it stays consistent everywhere:
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md). Common first stops:

- CUDA out of memory → reduce resolution/frames/steps, or use a smaller tier
- Hugging Face errors → verify license acceptance and token validity
- A workflow complains about a missing model → almost always a tier-naming
  mismatch, not an actual missing file

---

# Support

If you enjoy this project, please leave a ⭐ on GitHub.
