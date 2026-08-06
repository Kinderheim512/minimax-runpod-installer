# 🚀 MiniMax H3 RunPod Installer

<p align="center">

**One-command deployment of ComfyUI + MiniMax H3 on RunPod**

Automatically installs ComfyUI, MiniMax H3, official workflows, dependencies and optimizes everything for your GPU.

</p>

<p align="center">

![Platform](https://img.shields.io/badge/Platform-RunPod-blue)
![ComfyUI](https://img.shields.io/badge/ComfyUI-0.3+-green)
![Python](https://img.shields.io/badge/Python-3.11-yellow)
![CUDA](https://img.shields.io/badge/CUDA-12.8-success)
![License](https://img.shields.io/github/license/Kinderheim512/minimax-runpod-installer)

</p>

---

# ✨ Features

- 🚀 One-command installation
- 🎬 Automatic ComfyUI installation
- ⚡ Automatic CUDA / PyTorch configuration
- 🤖 Automatic MiniMax H3 installation
- 📥 Smart model downloader
- 🔁 Resume interrupted downloads
- 🧠 Intelligent model validation
- 🛠 Automatic repair of corrupted models
- 📦 Automatic installation of official MiniMax H3 workflows
- 🎨 Automatic LoRA installer
- 🌐 Hugging Face support
- 🌐 CivitAI support
- 📈 Automatic GPU optimization
- 🔄 Safe update system
- 📋 Installation verification tools
- 🖥 RunPod optimized

---

# 🎬 Supported Workflows

✅ Text → Video

✅ Image → Video

✅ Reference → Video

All official MiniMax H3 workflows are installed automatically.

---

# 📦 Installed Components

The installer automatically installs:

- ComfyUI
- ComfyUI Manager
- VideoHelperSuite
- PyTorch CUDA
- Hugging Face CLI
- hf_transfer
- hf_xet
- FFmpeg
- Git
- Aria2

---

# 🧠 MiniMax H3 Models

Automatically installs:

### Diffusion

- MiniMax H3 FL2VA INT8 Pruned
- MiniMax H3 REF2VA INT8 Pruned

### Text Encoder

- Qwen3VL 32B NVFP4 AWQ

### Video VAE

- MiniMax H3 Video VAE FP16

### Audio VAE

- MiniMax H3 Audio VAE FP32

The installer downloads only missing models and can automatically repair corrupted files.

---

# ⚡ Quick Start

Create a fresh RunPod and run:

```bash
cd /workspace

git clone https://github.com/Kinderheim512/minimax-runpod-installer.git

cd minimax-runpod-installer

bash bootstrap.sh
```

The installer automatically:

- Detects your GPU
- Installs ComfyUI
- Creates the Python environment
- Installs PyTorch
- Downloads MiniMax H3
- Installs the official workflows
- Optimizes ComfyUI
- Starts ComfyUI

No manual configuration required.

---

# 🎨 Installing LoRAs

```bash
bash install_lora.sh "https://..."
```

The LoRA is automatically installed into:

```
ComfyUI/models/loras/
```

---

# ⚡ Spectrum MiniMax H3 (optional)

[Spectrum MiniMax H3](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3) is an optional acceleration node for MiniMax H3.

- Enabled by default
- No additional Python dependencies
- Installs automatically into `custom_nodes/`
- Can be disabled by setting in `config.env`:

```bash
INSTALL_SPECTRUM=false
```

---

# 🔄 Updating

Updating is simple:

```bash
git pull

bash install.sh
```

Only missing or outdated components are updated.

Already downloaded models are never downloaded again.

---

# 🖇 Surviving Web Terminal Disconnects (tmux)

RunPod's web terminal can occasionally disconnect while a long-running task (model download, installation, generation...) is still running in the background. Without tmux, a disconnect can look like your work was lost — it wasn't, but there's no way to get back to it.

- `tmux` is installed automatically as part of the standard system dependencies.
- It is **never** started or used automatically by this installer — no script launches itself inside a tmux session.
- Using it is entirely optional and up to you.

To create or reattach to a session:

```bash
tmux new-session -A -s minimax
```

`-A` attaches to the `minimax` session if it already exists, or creates it if it doesn't — the same command works whether you're starting fresh or reconnecting after a disconnect. Detach with `Ctrl+b` then `d`; your session (and whatever is running inside it) keeps going in the background.

---

# 🖥 Recommended GPUs

| GPU | Recommended |
|------|------------|
| RTX A6000 | ✅ |
| RTX 6000 Ada | ✅ |
| L40S | ✅ |
| A100 80GB | ✅ |
| H100 | ✅ |

---

# 📸 Screenshots

*(Coming soon)*

- Installation
- ComfyUI
- Official workflows
- Generated videos

---

# 📚 Documentation

Detailed documentation is available:

- Installation Guide (English)
- Installation Guide (French)
- FAQ
- Troubleshooting
- Changelog

---

# 🛣 Roadmap

### v1.2

- LoRA manager
- Workflow installer
- Interactive CLI
- Backup & restore
- Model selector
- Plugin system

---

# 🤝 Contributing

Pull Requests are welcome.

If you find a bug or have a feature request, please open an Issue.

---

# 📜 License

Apache License 2.0

---

# ⭐ Support the project

If this project saved you time,

please consider giving it a ⭐ on GitHub.
