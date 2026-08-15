#!/usr/bin/env bash
# menu.sh — menu interactif regroupant toutes les actions du projet.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while true; do
  echo ""
  echo "  MiniMax H3 — RunPod / ComfyUI"
  echo "  ─────────────────────────────"
  echo "  0) Assistant de configuration puis installer (palier, preset, Turbo, SageAttention)"
  echo "  1) Installer (réglages par défaut/config.env)"
  echo "  2) Télécharger les modèles"
  echo "  3) Vérifier l'installation"
  echo "  4) Mettre à jour"
  echo "  5) Lancer ComfyUI"
  echo "  6) Lancer ComfyUI (tmux recommandé)"
  echo "  7) Arrêter ComfyUI (utile si un lancement précédent est resté bloqué)"
  echo "  8) Désinstaller"
  echo "  9) Quitter"
  echo ""
  read -r -p "  Choix [0-9] : " choice

  # "|| true" sur chaque sous-commande : sous set -e, un install.sh/check.sh/
  # uninstall.sh qui échoue (ou qu'on annule) tuerait sinon tout le menu au
  # lieu de revenir simplement à l'invite suivante.
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
    9) echo "Au revoir."; exit 0 ;;
    *) echo "Choix invalide." ;;
  esac
done
