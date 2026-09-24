# MiniMax H3 Launcher GUI

The launcher's desktop front-end: one window to rent the pod, open the SSH
tunnel, start ComfyUI (or the LoRA-training desktop) and open it in your
browser, manage credentials, and run diagnostics or repairs — without touching
the command line.

It is a thin front-end. Everything it does goes through the same primitives
the CLI uses (`launcher.orchestrator`, `launcher.infra_diagnose`,
`launcher.doctor`, `launcher.infra_recover`, `launcher.credentials`), so the
two can never describe the stack differently.

Stdlib only at runtime, plus one optional styling dependency:

```bash
python -m pip install ".[themes]"     # ttkbootstrap: themed controls (optional)
```

With `ttkbootstrap` installed the window uses the ready-made theme family and
the settings dialog's appearance switch flips it between the dark and light
variants live (no restart). Without it the GUI falls back to its built-in
palettes — same layout, same behaviour.

## Launching

### From source

```bash
python -m launcher gui
# or, with the console script:
minimax-launcher gui
```

### Standalone binary

```bash
python -m pip install ".[gui-build]"     # PyInstaller (build-time only)
python scripts/build_exe.py              # -> dist/MiniMaxH3Launcher[.exe]
```

The release workflow builds this for Windows, macOS (Apple silicon) and Linux;
see the README for the download table and the unsigned-binary workaround.

## First run

With nothing configured, the launcher opens the credentials dialog by itself
(once per session). Paste your RunPod API key, press *Save*, pick **ComfyUI
video** in the footer and press **Start**. The comfy stack deploys the public
template, so there is no private template ID to create — a private one, if you
have it, simply wins.

## Tabs

| Tab | What it holds |
| --- | --- |
| **Dashboard** | The health table, the pod/billing card, the active-stacks panel and the journal |
| **ComfyUI** | The ComfyUI stack: preset, tier, workflows, access mode, automatic collection |
| **LoRA training** | The training stack: image, desktop/files URLs, pre-downloaded model families, LoRA collection |
| **Models & presets** | The model catalog and the preset editor (models / custom nodes / workflows) |
| **Library** | LoRA / Workflow / Node entries, from a URL or a local file |
| **Settings** | `.env`, the timer, and the configuration recap |

## Features

- **Dashboard (auto-refresh, ~5 s, toggleable)** — one row per component
  (`Pod`, `SSH tunnel`, the stack's service, its preset or image,
  `Configuration`) plus the overall grade (`HEALTHY` / `DEGRADED` / `FAILED` /
  `STOPPED`) with the cause and the remediation. The start action is visually
  distinct from the diagnostic and destructive ones, so terminating a pod is
  never confused with a normal stop.
- **Billing row** — how long the pod has been running, the estimated spend
  (elapsed time × hourly rate), the hourly rate, and the RunPod wallet balance
  with an estimate of how long the credits last. The balance comes from the
  RunPod GraphQL `myself` query (`clientBalance` — the REST v2 API does not
  expose it). The row turns orange under ~6 h of credits left and red under
  ~2 h, and keeps its last values while a probe transiently fails.
- **Finding an open stack** — the refresh control (and the startup refresh)
  re-adopts a stack opened from another session (an earlier launcher instance,
  the CLI, another machine sharing the account): when this session has no
  registered pod for the active stack, every RUNNING launcher-managed pod on
  the account (matched by its `minimax-launcher-<stack>-` name or its
  environment) is listed. The one matching the active stack is re-adopted
  automatically — bookkeeping only, no pod action — and an open stack of the
  *other* stack is offered with an *Adopt* button.
- **Active stacks** — one row per workload stack, each with its pod status,
  tunnel, readiness and hourly rate, its own Start / Stop / Terminate buttons,
  and the cumulative cost per hour underneath. Both stacks can run at once,
  each on its own pod, tunnel and lifecycle; starting one never touches the
  other, and a running stack's row disables its own Start (no double
  provisioning).
- **Start** — the full sequence: provision the pod when needed → SSH tunnel →
  the stack's service → browser. Long operations run on a worker thread, so
  the window stays responsive.
- **Stop / Terminate the pod** — Stop closes the local processes and stops the
  pod, keeping its disk. Terminate (right next to it, and never next to *Quit*)
  additionally deletes the pod, with confirmation, to free the GPU.
- **ComfyUI stack** — preset, tier (auto / auto + personal / personal),
  workflows, access mode (tunnel by default, direct as an explicit option),
  sage attention, the ntfy topic, the private-vault repo, and the ComfyUI
  version pin (fetched from the GitHub releases list, with a local fallback).
- **Automatic collection** — the launcher watches the ComfyUI queue through
  the tunnel; each finished generation is downloaded into the chosen folder
  together with a `.txt` (prompt, settings, resources, workflow). The
  checkboxes are independent extras, and *Stop the pod once the queue is
  empty* only fires after the queue has stayed empty for a settle window
  (60 s by default).
- **LoRA training stack** — the resolved image, desktop and file-manager URLs,
  the registered pod, *Open Fizgig ↗* and *Files ↗*, *Verify the template*,
  the model families to pre-download at boot (~45 GB, so the cost is visible
  and off by default), the local LoRA collection folder, and the
  collect/list actions. The health table speaks the stack's own vocabulary
  (Desktop / Image).
- **Library (LoRA / Workflow / Node)** — a card per asset, each with a
  checkbox: checked means the asset is pushed to the pod at creation
  (`H3_CUSTOM_LORAS` / `_NODES` / `_WORKFLOWS`), unchecked means it stays in
  the library but is never sent to a new pod — it can still be installed on
  demand. A local file or folder entry is *uploaded by the launcher* over the
  already-open SSH tunnel (the pod cannot read your disk, and the pod-side
  installer only accepts http(s)); a file already present with the same size
  is skipped. The upload happens after ComfyUI is open, is non-blocking, and
  the journal reports name, size and outcome.
- **Doctor** — non-destructive diagnostics, printed into the in-window
  journal.
- **Repair** — *Start the pod*, *Reconnect SSH*, *Restart ComfyUI*,
  *Restart the training*. The row is only shown when
  `LAUNCHER_INFRA_RECOVERY=confirm`; every action asks for confirmation and
  reports before/after. The one-click shortcut next to the
  « Cause → Remediation » hint maps the failing component onto the matching
  action, and stays hidden when nothing maps (configuration) or while another
  action is running.
- **Credentials** — shows the store state without values; *Configure* (masked
  fields, stored in the OS credential store, an empty field keeps the stored
  value) and *Clear* (with confirmation).
- **`.env`** — shows which `.env` file is in use (or *none (optional)*),
  creates a starter file, and opens it in the default editor.
- **Timer** — enter a duration in minutes and start; when it expires the
  launcher stops every registered pod (plus the local tunnels), with retries,
  and only reports success once no pod is billing any more. The deadline is
  absolute and persisted, so a stop missed while the launcher was closed fires
  on the next start; an optional *after the pod stops* action can then shut
  the machine down or put it to sleep, executed only once the stop is
  confirmed.
- **Journal** — the live launcher log. Every line passes through the standard
  secret redaction, so no API key or token ever appears.
- **Settings dialog** — appearance, language (English / French), automatic
  refresh, close-to-tray, the API keys and the per-stack template IDs.
- **Icon** — the title bar *and* the taskbar use the native multi-size brand
  icon (`launcher/icon.ico`, 16–256 px; embedded in the frozen build), so the
  app never shows the default `pythonw` icon.
- **No console window** — the frozen build is windowed, and every child
  process (the SSH tunnel, `scp`, the pod probes) is spawned console-free.
- **Tray** — when `pystray` is installed (extra `[tray]`), closing the window
  minimizes to a tray icon with a menu: Show / Start / Stop / Quit. Without
  pystray, closing the window quits, and the checkbox is disabled with the
  reason in the journal.

## Configuration

The GUI reads the same environment variables as the CLI. On top of that, at
startup it loads a `.env` file if one is found, in this order:

1. `MINIMAX_LAUNCHER_ENV_FILE` (explicit path override);
2. the executable's directory (frozen build) — for a source run, the
   repository root;
3. the current working directory.

Variables already present in the environment always win. RunPod secrets come
first from the environment, then from the OS credential store
(`minimax-launcher credentials set`). GUI choices live in
`%APPDATA%\MiniMaxH3Launcher\gui.json` on Windows
(`~/Library/Application Support/MiniMaxH3Launcher/` on macOS,
`$XDG_DATA_HOME/minimax-launcher/` on Linux).

## Build layout

| File | Purpose |
|---|---|
| `launcher/gui.py` | The GUI (tkinter) plus the non-UI logic (env loading, settings, status snapshot, actions) |
| `launcher/main.py` | CLI wiring (`minimax-launcher gui`) |
| `launcher_gui_entry.py` | PyInstaller target script (calls `launcher.gui.main`) |
| `scripts/build_exe.py` | PyInstaller build → `dist/MiniMaxH3Launcher` (one file, no console) |
| `scripts/verify_exe.py` | Verifies the built binary on its packaged bytecode |
| `scripts/make_launcher_icon.py` | Regenerates `icon.png` / `icon.ico` / `icon.icns` from one master |

## Tests

`python -m pytest tests/test_gui.py` covers the non-UI logic (env-file
parsing and discovery, settings persistence, log redaction, status snapshot
mapping, action delegation, CLI dispatch) and runs a tkinter smoke test,
skipped automatically when no display is available.
