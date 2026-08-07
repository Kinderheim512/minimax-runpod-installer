# Using tmux with this project

## Why tmux matters on RunPod

RunPod's web terminal can disconnect while a long-running task is still
running in the background — a model download, `install.sh`, a video
generation. When that happens, the browser tab closes or reloads, and it
*looks* like your work was lost. It usually wasn't: the process may still be
running on the pod. The problem is that once the terminal that started it is
gone, you have no way back into it.

`tmux` solves this by decoupling your terminal session from your browser
connection. A tmux session keeps running on the pod itself, independent of
whether anything is attached to it. You can detach from it on purpose, lose
the connection by accident, close the tab, or even close your laptop — the
session and everything running inside it keeps going. Reconnect later and
pick up exactly where you left off.

## How this project uses it

- `tmux` is installed automatically as part of the standard system
  dependencies (`lib/system.sh`), alongside `git`, `curl`, `ffmpeg`, and
  `aria2`.
- **`bootstrap.sh` — the recommended entry point — launches ComfyUI inside
  tmux automatically.** Its last line is `exec ./launch.sh --tmux`, so a
  fresh `bash bootstrap.sh` always ends with ComfyUI running inside a
  persistent tmux session named `minimax`, not in the foreground of your
  current shell.
- `install.sh` itself does **not** start tmux — only `launch.sh --tmux`
  (and therefore `bootstrap.sh`, and menu option 6) does. If you call
  `install.sh` directly and it happens to run long enough that your
  terminal disconnects, see [Protecting `install.sh` manually](#protecting-installsh-manually)
  below.
- Using tmux at all remains optional: `bash launch.sh` (no flag) still
  launches ComfyUI directly in the foreground, exactly as before.

## Launching ComfyUI in tmux

### Recommended: through `bootstrap.sh` or the menu

```bash
bash bootstrap.sh
```

does the full install/update and finishes with `launch.sh --tmux`
automatically. For an already-installed pod, use the menu:

```bash
bash menu.sh
```

and choose:

```
6) Lancer ComfyUI (tmux recommandé)
```

(equivalent to `bash launch.sh --tmux`). Either path:

- creates the `minimax` tmux session and launches ComfyUI inside it, if the
  session doesn't exist yet;
- reattaches to that same session instead, if it already exists — nothing
  gets relaunched;
- detects if ComfyUI is already running (`http://127.0.0.1:8188`) and never
  starts a second instance;
- always uses the same Python virtual environment the installer
  configured — you never activate it yourself.

If your current shell isn't an interactive terminal (e.g. a non-interactive
`bootstrap.sh` run via RunPod's "Start Command", or `curl | bash`), the
session is still created and ComfyUI still starts inside it — `launch.sh`
just skips trying to attach and prints the command to reattach later
instead of failing.

Detach anytime with `Ctrl+b` then `d`; ComfyUI keeps running. Run
`bash launch.sh --tmux` (or menu option 6) again — even from a brand-new
terminal after a disconnect — to reattach.

### Manual usage

You can still manage your own tmux session directly if you prefer full
control, or want to protect something other than the ComfyUI launch itself:

```bash
tmux new-session -A -s minimax
```

`-A` attaches to the `minimax` session if it already exists, or creates it
if it doesn't — the same command works whether you're starting fresh or
reconnecting. Note this uses the **same session name** (`minimax`) as
`launch.sh --tmux`, so the two approaches interoperate: attaching manually
will drop you into the same session ComfyUI was auto-launched in, if it was.

## Protecting `install.sh` manually

`install.sh` (called on its own, not through `bootstrap.sh`) does not wrap
itself in tmux. If you're running it directly and want it protected against
a terminal disconnect during a long model download, start tmux first:

```bash
tmux new-session -A -s minimax
bash install.sh
```

If the web terminal drops while models are downloading, nothing is lost.
Reconnect to the pod and run:

```bash
tmux new-session -A -s minimax
```

to land back inside the same session, watching the same install continue
(or already finished). `install.sh` also resumes interrupted model
downloads on its own, independent of tmux — see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#download-interrupted-network-drop-pod-restart-terminal-disconnect).

## Detaching, reattaching, and cleaning up

| Action | Command |
|---|---|
| Detach (leave session running, return to normal terminal) | `Ctrl+b` then `d` |
| Reattach (or create if it doesn't exist) | `tmux new-session -A -s minimax` or `bash launch.sh --tmux` |
| List running sessions | `tmux ls` |
| Kill the session entirely | `tmux kill-session -t minimax` |

Detaching (`Ctrl+b d`) is not the same as closing the terminal tab by
accident — both leave the session running, but detaching is the clean,
intentional way to step away.

## FAQ

**Do I have to use tmux?**
No. `bash launch.sh` without `--tmux` still runs ComfyUI directly in the
foreground.

**Will `bootstrap.sh` always start tmux for me?**
Yes — that's its last step, on purpose, so a first-time install survives a
disconnect without any extra action from you.

**What if I run `install.sh` directly (not through `bootstrap.sh`) and the
terminal disconnects?**
If the process was killed along with the terminal, re-run `install.sh` —
completed steps are skipped and model downloads resume automatically, so
you won't lose completed files even if a step has to restart. Wrapping it
in `tmux new-session -A -s minimax` first avoids the interruption
altogether.
