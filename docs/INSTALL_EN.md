# Installation Guide

This guide explains how to deploy **ComfyUI + MiniMax H3** on a fresh RunPod.

---

# Requirements

Before starting, make sure you have:

- A RunPod account
- A Hugging Face account
- Accepted the MiniMax H3 license

https://huggingface.co/Comfy-Org/MiniMax-H3

You will also need a Hugging Face Access Token with **Read** permission.

---

# Recommended GPUs

| GPU | Status |
|------|--------|
| RTX A6000 | ✅ Recommended |
| RTX 6000 Ada | ✅ Recommended |
| L40S | ✅ Recommended |
| A100 80GB | ✅ |
| H100 | ✅ |

Minimum recommended VRAM:

**48 GB**

---

# Step 1 — Create a RunPod

Create a new RunPod.

Recommended template:

- PyTorch
- CUDA 12.x
- Ubuntu

Expose port:

8188 (HTTP)

---

# Step 2 — Open the terminal

Run:

```bash
cd /workspace

git clone https://github.com/Kinderheim512/minimax-runpod-installer.git

cd minimax-runpod-installer

bash bootstrap.sh
```

---

# Step 3 — Wait

The installer automatically:

- Detects your GPU
- Creates a Python virtual environment
- Installs PyTorch
- Installs ComfyUI
- Installs ComfyUI Manager
- Installs VideoHelperSuite
- Creates every required model folder
- Downloads MiniMax H3
- Installs the official workflows
- Optimizes ComfyUI
- Starts ComfyUI

No manual configuration is required.

---

# Step 4 — Hugging Face

If required, enter your Hugging Face token.

The installer automatically verifies that you accepted the MiniMax H3 license.

---

# Step 5 — Open ComfyUI

Open your browser.

Your RunPod URL looks like:

https://YOUR-POD-ID-8188.proxy.runpod.net

---

# Included Workflows

The installer automatically installs:

- Text to Video
- Image to Video
- Reference to Video

No manual import is required.

---

# Install a LoRA

Download a LoRA directly:

```bash
bash install_lora.sh "YOUR_URL"
```

Example:

```bash
bash install_lora.sh "https://civitai.red/api/download/models/XXXX?fileId=XXXX"
```

The LoRA is automatically installed into:

```
ComfyUI/models/loras/
```

---

# Updating

Updating is simple.

```bash
git pull

bash install.sh
```

The installer downloads only missing files.

Already installed models are skipped automatically.

---

# Verify the installation

```bash
bash check.sh
```

Checks:

- GPU
- CUDA
- PyTorch
- MiniMax models
- Disk space
- Workflows

---

# Troubleshooting

## Missing models

Run:

```bash
bash install.sh
```

The installer automatically repairs missing models.

---

## CUDA Out Of Memory

Reduce:

- Resolution
- Number of frames
- Steps

or use a GPU with more VRAM.

---

## Hugging Face error

Verify:

- License accepted
- Token validity

---

# Support

If you enjoy this project,

please leave a ⭐ on GitHub.