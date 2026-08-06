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
whether anything is attached to it. You can detach from it on purpose,
lose the connection by accident, close the tab, or even close your laptop —
the session and everything running inside it keeps going. Reconnect later
and pick up exactly where you left off.

## How this project uses it

- `tmux` is installed automatically as part of the standard system
  dependencies (`lib/system.sh`), alongside `git`, `curl`, `ffmpeg`, and
  `aria2`.
- It is **never** started or used automatically by any script in this
  project. No script launches itself inside a tmux session for you.
- Using it is entirely optional. Everything (`install.sh`, `update.sh`,
  `menu.sh`, `launch.sh`...) works exactly the same with or without it.

In short: tmux is available the moment your pod is ready, and it's there
if you want it — nothing changes if you don't.

## The one command you need

```bash
tmux new-session -A -s minimax
```

`-A` means *attach if the session exists, otherwise create it*. This is
the only command you need to remember — it works identically whether
you're starting fresh or reconnecting after a disconnect.

## Recommended workflow for this project

Start tmux **before** running anything long, not after — that's what
actually protects a download or install in progress:

```bash
tmux new-session -A -s minimax
bash install.sh
```

If the web terminal drops while `install.sh` is downloading the H3 models,
nothing is lost. Reconnect to the pod and run the same command again:

```bash
tmux new-session -A -s minimax
```

You'll be back inside the same session, watching the same install continue
(or already finished).

The same applies to launching ComfyUI so it survives a disconnect:

```bash
tmux new-session -A -s minimax
bash launch.sh          # or: bash menu.sh -> option 5
```

## Detaching, reattaching, and cleaning up

| Action | Command |
|---|---|
| Detach (leave session running, return to normal terminal) | `Ctrl+b` then `d` |
| Reattach (or create if it doesn't exist) | `tmux new-session -A -s minimax` |
| List running sessions | `tmux ls` |
| Kill the session entirely | `tmux kill-session -t minimax` |

Detaching (`Ctrl+b d`) is not the same as closing the terminal tab by
accident — both leave the session running, but detaching is the clean,
intentional way to step away.

## FAQ

**Do I have to use tmux?**
No. It's a convenience, not a requirement. Every script in this project
runs fine directly in the web terminal.

**Will an install script ever start tmux for me?**
No, on purpose. You decide when to use it.

**What if I forget to start tmux before a long task and the terminal
disconnects?**
If the process was killed along with the terminal, you'll need to restart
it — next time, start `tmux new-session -A -s minimax` first. `install.sh`
resumes interrupted model downloads automatically, so you won't lose
completed files even if a step has to restart.
