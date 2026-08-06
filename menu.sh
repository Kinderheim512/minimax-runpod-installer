#!/usr/bin/env bash
# menu.sh — menu interactif regroupant toutes les actions du projet.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while true; do
  echo ""
  echo "  MiniMax H3 — RunPod / ComfyUI"
  echo "  ─────────────────────────────"
  echo "  1) Installer"
  echo "  2) Télécharger les modèles"
  echo "  3) Vérifier l'installation"
  echo "  4) Mettre à jour"
  echo "  5) Lancer ComfyUI"
  echo "  6) Lancer ComfyUI (tmux recommandé)"
  echo "  7) Désinstaller"
  echo "  8) Quitter"
  echo ""
  read -r -p "  Choix [1-8] : " choice

  case "$choice" in
    1) bash "${PROJECT_ROOT}/install.sh" ;;
    2) bash "${PROJECT_ROOT}/install.sh" --only-models ;;
    3) bash "${PROJECT_ROOT}/check.sh" ;;
    4) bash "${PROJECT_ROOT}/update.sh" ;;
    5) bash "${PROJECT_ROOT}/launch.sh" ;;
    6) bash "${PROJECT_ROOT}/launch.sh" --tmux ;;
    7) bash "${PROJECT_ROOT}/uninstall.sh" ;;
    8) echo "Au revoir."; exit 0 ;;
    *) echo "Choix invalide." ;;
  esac
done
