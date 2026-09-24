# 🚀 MiniMax H3 RunPod Installer

<p align="center">

**Deploy ComfyUI + MiniMax H3 on RunPod — from a desktop app, or by hand**

The launcher rents the GPU, opens the tunnel and puts ComfyUI in your browser.
The installer does the same thing from the pod's terminal, if you prefer to
drive it yourself.

</p>

<p align="center">

![Platform](https://img.shields.io/badge/Platform-RunPod-blue)
![ComfyUI](https://img.shields.io/badge/ComfyUI-%3E%3D0.30.0-green)
![Python](https://img.shields.io/badge/Python-3.10%2B-yellow)
![CUDA](https://img.shields.io/badge/CUDA-auto--detected%20(11.8–13.0)-success)
![Launcher](https://img.shields.io/badge/launcher-Windows%20%7C%20macOS%20%7C%20Linux-blueviolet)
![License](https://img.shields.io/github/license/Kinderheim512/minimax-runpod-installer)

</p>

<p align="center">

**👉 <a href="https://console.runpod.io/hub/template/oa2vozqbum?ref=76jvawoy">Deploy the MiniMax H3 template on RunPod</a>**

</p>

---

## Contents

- [Desktop launcher](#-desktop-launcher)
- [Manual install](#-manual-install-the-pod-side-installer)
- [Project structure](#-project-structure)
- [Documentation](#-documentation)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [License](#-license)
- [Support the project](#-support-the-project)

---

## 🖥️ Desktop launcher

A self-contained desktop app that rents the pod, opens the SSH tunnel, starts
ComfyUI (or the LoRA-training desktop) and opens the UI in your browser. It
runs on **Windows, macOS and Linux**, and a first run needs nothing but a
RunPod API key — the template it deploys is public, so there is no private
template ID to look up.

It ships in English (US) and French; switch language in *Settings*.

**What it gives you**

- 🖱 **Click and go** — a first run is: paste your API key, press Start
- 🔐 **No public port** — ComfyUI is reached through an SSH tunnel, never
  exposed on the pod's public URL (an explicit *direct* mode exists if you
  want it)
- 📓 **Live journal** — every provisioning step narrated, with the pod's
  status, VRAM and spend on the dashboard
- 🎬 **ComfyUI stack** — pick a preset, a model tier and the workflows to
  install; the launcher syncs the pod to that selection
- 🧪 **LoRA training stack** — rents a GPU, streams the Fizgig desktop over
  KasmVNC and collects the trained LoRAs back to your machine
- 📚 **Library** — install LoRAs, workflows and custom nodes from a URL or a
  local file, and keep them across pod recreations
- 🔔 **Collect finished generations** — watch the ComfyUI queue through the
  tunnel and pull the outputs down automatically
- ⏱ **Timer** — stop the pod after N minutes, optionally shut the machine down
- 🩺 **Diagnostics** — `doctor` and the Repair button explain what is wrong and
  fix the recoverable cases

### Install

| OS | Download | Run |
| --- | --- | --- |
| **Windows 10/11** | `MiniMaxH3Launcher-windows-x64.exe` | double-click |
| **macOS (Apple silicon + Intel)** | `MiniMaxH3Launcher-macos-universal2` | `chmod +x … && ./…` |
| **Linux x64** | `MiniMaxH3Launcher-linux-x64` | `chmod +x … && ./…` |

Grab them from the [releases page](https://github.com/Kinderheim512/minimax-runpod-installer/releases).

**Prerequisites, all platforms**

- a **RunPod account** with an API key
  ([console → Settings → API Keys](https://www.runpod.io/console/user/settings));
- an **SSH key registered on RunPod**. The launcher forwards ComfyUI over SSH,
  so it needs the OpenSSH client: Windows 10+ ships it, macOS and Linux have
  it. `doctor` tells you if it is missing.

### First run

1. Launch the app. It opens the credentials dialog on its own.
2. Paste your **RunPod API key** and press *Save*. Everything else is
   optional: a private template ID, an HF token, a Civitai key, the
   training-desktop password.
3. Pick **ComfyUI video** in the footer and press **Start**. The first boot
   pulls the image and downloads the preset's weights, so give it the time it
   asks for — the journal narrates every step.
4. ComfyUI opens in your browser once it answers.

From a terminal, the same thing:

```bash
python -m launcher credentials set     # store the RunPod API key
python -m launcher start --stack comfy
python -m launcher status
python -m launcher doctor
```

### Where your secrets live

The RunPod API key and the optional extra keys go into the operating system's
own credential store — **DPAPI** on Windows, the **login Keychain** on macOS,
the **Secret Service** (`secret-tool`) on Linux. Never a plain file, never the
repository. On a Linux box with no Secret Service the launcher falls back to a
`0600` file under `~/.local/share/minimax-launcher/`, and says so in its log.

### Unsigned binaries

The released binaries are **not code-signed** (a certificate costs money and
this is a free tool), so each OS asks once:

- **macOS** — Gatekeeper quarantines anything downloaded through a browser:

  ```bash
  xattr -dr com.apple.quarantine MiniMaxH3Launcher-macos-universal2
  ```

- **Windows** — SmartScreen shows "Windows protected your PC": *More info* →
  *Run anyway*.

Building from source avoids both:

```bash
python -m pip install pyinstaller pillow
python scripts/build_exe.py
```

### What the launcher does *not* do

It deploys and drives the pod; it never trains and never generates on your
machine. The training stack only opens the desktop — the work itself happens
on the rented GPU.

---

## 📦 Manual install (the pod-side installer)

The scripts in this repository install ComfyUI + MiniMax H3 *on the pod*, from
the pod's own terminal. Same result, no desktop app — useful for scripted
deployments, for a pod you already have, or if you would rather see every
command.

The quickest route is the ready-made RunPod template:

**👉 [Deploy the MiniMax H3 template on RunPod](https://console.runpod.io/hub/template/oa2vozqbum?ref=76jvawoy)**

Or, on a pod you already have:

```bash
git clone https://github.com/Kinderheim512/minimax-runpod-installer.git
cd minimax-runpod-installer
bash wizard.sh          # interactive
# or
bash bootstrap.sh       # one command, non-interactive
```

Full walkthrough, and the reference for tiers, presets, workflows, the
pre-installed Docker image, backups and the CLI:
**[docs/INSTALL_EN.md](docs/INSTALL_EN.md)** ·
**[docs/INSTALL_FR.md](docs/INSTALL_FR.md)**.

---

## 📁 Project structure

```
.
├── launcher/              # desktop app: tkinter GUI + CLI (vendored, see NOTICE)
│   ├── gui.py             # the GUI
│   ├── main.py            # the CLI (python -m launcher)
│   ├── orchestrator.py    # pod -> tunnel -> service startup sequence
│   ├── credentials.py     # DPAPI / Keychain / Secret Service store
│   ├── comfy_*.py         # ComfyUI operations, presets, versions, watcher
│   ├── train_*.py         # LoRA-training stack
│   └── locales/           # English (source) + French tables
├── tests/                 # pytest suite (1000 tests)
├── scripts/
│   ├── build_exe.py       # PyInstaller build
│   └── make_launcher_icon.py  # icon.png / icon.ico / icon.icns
├── wizard.sh              # interactive setup wizard (manual path)
├── bootstrap.sh           # one-command, non-interactive entry point
├── install.sh             # full installer (see CLI reference)
├── update.sh              # update ComfyUI/nodes/PyTorch without touching models
├── check.sh               # read-only verification
├── launch.sh              # start ComfyUI (optionally in tmux)
├── menu.sh                # interactive menu wrapping the scripts above
├── uninstall.sh           # remove ComfyUI (optionally keep models/)
├── install_lora.sh        # standalone LoRA install/list/remove
├── sync_push.sh           # manual push of personal LoRAs/presets/outputs to the HF vault
├── config.env             # central configuration (paths, tiers, sources, ...)
├── requirements.txt       # project-level Python deps (on top of ComfyUI's own)
├── pyproject.toml         # launcher packaging (entry points, extras)
├── MiniMaxH3Launcher.spec # PyInstaller spec used by the release workflow
├── Dockerfile             # pre-installed Docker image (see INSTALL_EN.md)
├── docker-build-steps-heavy.sh  # build-time (no-GPU): apt/CUDA/ComfyUI clone/venv/deps/PyTorch/SageAttention wheel
├── docker-build-steps-light.sh  # build-time (no-GPU): ComfyUI-Manager/custom nodes/model folders
├── docker-entrypoint.sh   # container entrypoint: installs PyTorch, then runs install.sh + launch.sh
├── image-contract.json    # what the baked image guarantees (enforced by tests)
├── lib/
│   ├── utils.sh           # logging, error handling, step tracking, retries
│   ├── system.sh          # apt package installation
│   ├── gpu.sh             # GPU/VRAM/CUDA detection, tier recommendation
│   ├── python.sh          # venv, PyTorch build selection & install, CUDA checks
│   ├── comfyui.sh         # clone/update the ComfyUI repo itself
│   ├── manager.sh         # ComfyUI-Manager install/update
│   ├── nodes.sh           # optional custom nodes (VideoHelperSuite, Spectrum, ...)
│   ├── huggingface.sh     # HF auth + gated-repo access check
│   ├── download.sh        # generic HF file download (hf-cli, resume, verify)
│   ├── models.sh          # H3 tier/workflow resolution, manifest, download orchestration
│   ├── workflows.sh       # copies workflow JSON matching the current selection
│   ├── presets.sh         # extra per-workflow model sets, nodes, symlinks
│   ├── optimization.sh    # GPU-tuned ComfyUI launch flags
│   ├── personal_storage.sh # LoRAs/presets/outputs backup & restore
│   ├── annuaire.sh        # Library: install LoRAs/workflows/nodes on the pod
│   ├── user_presets.sh    # user-defined presets, persisted on the pod
│   ├── notify.sh          # ntfy.sh push notifications
│   └── verify.sh          # check.sh backend + install summary
├── workflows/             # official MiniMax H3 workflow JSON files
├── presets/               # preset-specific workflow JSON files
│   ├── dasiwa_mmh3v12/
│   └── muse_director_seedhunt/
├── docs/
│   ├── INSTALL_EN.md      # install guide + full installer reference (English)
│   ├── INSTALL_FR.md      # guide d'installation (Français)
│   └── launcher/          # launcher internals (stack, credentials, GUI, pods)
├── .github/workflows/     # CI (bash -n, ShellCheck, JSON, pytest) + release build
├── TMUX.md
├── FAQ.md
├── TROUBLESHOOTING.md
└── CHANGELOG.md
```

---

## 📚 Documentation

* [Installation Guide — English](docs/INSTALL_EN.md) — includes the full
  installer reference (tiers, presets, workflows, Docker image, CLI)
* [Guide d'installation — Français](docs/INSTALL_FR.md)
* [Launcher internals](docs/launcher/README.md) — stacks, credentials, pods, GUI
* [FAQ](FAQ.md)
* [Troubleshooting](TROUBLESHOOTING.md)
* [Using tmux with this project](TMUX.md)
* [Changelog](CHANGELOG.md)

---

## 🛣 Roadmap

* Screenshots of the launcher and the install flow
* Backup & restore helper for `models/`
* Plugin system for optional custom nodes beyond `config.env`'s static list

---

## 🤝 Contributing

Pull requests are welcome. If you find a bug or have a feature request,
please open an issue.

---

## 📜 License

The pod-side installer (this repository's `install.sh`, `lib/`, `Dockerfile`,
`workflows/`, `presets/`) is **Apache License 2.0** — see [LICENSE](LICENSE).

The `launcher/` package is vendored from
[OpenFox Forge](https://github.com/Kinderheim512/openfox-forge) under the
**MIT** license; see [NOTICE](NOTICE) for the attribution and the
resynchronisation procedure.

---

## ⭐ Support the project

If this project saved you time, please consider giving it a ⭐ on GitHub.

Deploying through the template link below supports the project at no extra
cost to you:

**👉 [Deploy the MiniMax H3 template on RunPod](https://console.runpod.io/hub/template/oa2vozqbum?ref=76jvawoy)**

**👉 [runpod.io](https://runpod.io?ref=76jvawoy)**
