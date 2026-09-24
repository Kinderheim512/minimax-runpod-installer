# Launcher documentation

These pages document `launcher/`, the desktop front-end (tkinter GUI + CLI)
that rents the pod, opens the SSH tunnel and drives the stack. They are
vendored from MiniMax H3 Launcher alongside the code (see `NOTICE`); the
text-agent, llama.cpp and SearXNG pages of that project are deliberately
absent — those stacks are not part of this launcher.

| Page | What it covers |
| --- | --- |
| [comfy-stack.md](comfy-stack.md) | The ComfyUI + MiniMax H3 stack end to end |
| [train-stack.md](train-stack.md) | The LoRA-training stack (Fizgig over KasmVNC) |
| [train-stack-validation.md](train-stack-validation.md) | How the training stack was validated live |
| [credentials.md](credentials.md) | Where secrets live and how they are resolved |
| [pod-provisioning.md](pod-provisioning.md) | Pod resolution, provisioning and the registry |
| [gui.md](gui.md) | The GUI's structure, threading model and conventions |
