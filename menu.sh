#!/usr/bin/env bash
# menu.sh — interactive menu grouping all the project's actions.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while true; do
  echo ""
  echo "  MiniMax H3 — RunPod / ComfyUI"
  echo "  ─────────────────────────────"
  echo "  0) Configuration assistant then install (tier, preset, SageAttention [discouraged])"
  echo "  1) Install (default settings/config.env)"
  echo "  2) Download models"
  echo "  3) Check the installation"
  echo "  4) Update"
  echo "  5) Launch ComfyUI"
  echo "  6) Launch ComfyUI (tmux recommended)"
  echo "  7) Stop ComfyUI (useful if a previous launch got stuck)"
  echo "  8) Uninstall"
  echo "  9) Quit"
  echo ""
  read -r -p "  Choice [0-9] : " choice

  # "|| true" on each sub-command: under set -e, a failing (or cancelled)
  # install.sh/check.sh/uninstall.sh would otherwise kill the whole menu
  # instead of simply returning to the next prompt.
  case "$choice" in
    0) bash "${PROJECT_ROOT}/wizard.sh" || true ;;
    1) bash "${PROJECT_ROOT}/install.sh" || true ;;
    2) bash "${PROJECT_ROOT}/install.sh" --only-models || true ;;
    3) bash "${PROJECT_ROOT}/check.sh" || true ;;
    4) bash "${PROJECT_ROOT}/update.sh" || true ;;
    5) bash "${PROJECT_ROOT}/launch.sh" || true ;;
    6) bash "${PROJECT_ROOT}/launch.sh" --tmux || true ;;
    7) bash "${PROJECT_ROOT}/launch.sh" --stop || true ;;
    8) bash "${PROJECT_ROOT}/uninstall.sh" || true ;;
    9) echo "Goodbye."; exit 0 ;;
    *) echo "Invalid choice." ;;
  esac
done
