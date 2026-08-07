#!/usr/bin/env bash
# ==============================================================================
# bootstrap.sh
# Bootstrap automatique MiniMax H3 + ComfyUI pour RunPod
# ==============================================================================

set -Eeuo pipefail

REPO_URL="https://github.com/Kinderheim512/minimax-runpod-installer.git"
INSTALL_DIR="/workspace/minimax-runpod-installer"
COMFY_DIR="/workspace/ComfyUI"

echo
echo "=============================================================="
echo "           MiniMax H3 Bootstrap for RunPod"
echo "=============================================================="
echo

# --------------------------------------------------------------------------
# Vérification Git
# --------------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "[ERREUR] Git n'est pas installé."
    exit 1
fi

# --------------------------------------------------------------------------
# Clone ou mise à jour
# --------------------------------------------------------------------------

if [[ ! -d "$INSTALL_DIR/.git" ]]; then

    echo "[INFO] Clonage du dépôt GitHub..."

    git clone "$REPO_URL" "$INSTALL_DIR"

else

    echo "[INFO] Dépôt déjà présent."

    cd "$INSTALL_DIR"

    echo "[INFO] Mise à jour..."

    git fetch origin

    git reset --hard origin/main

fi

cd "$INSTALL_DIR"

# --------------------------------------------------------------------------
# Droits
# --------------------------------------------------------------------------

find . -name "*.sh" -exec chmod +x {} \;

# --------------------------------------------------------------------------
# Vérification HF_TOKEN
# --------------------------------------------------------------------------

if [[ -z "${HF_TOKEN:-}" ]]; then

    echo
    echo "[ATTENTION] HF_TOKEN n'est pas défini."
    echo
    echo "Les modèles MiniMax H3 ne pourront pas être téléchargés."
    echo
    echo "Configure un Secret RunPod nommé HF_TOKEN."
    echo

fi

# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------

if [[ ! -f "$COMFY_DIR/main.py" ]]; then

    echo
    echo "[INFO] Première installation..."
    echo

    ./install.sh

else

    echo
    echo "[INFO] ComfyUI déjà installé."
    echo "[INFO] Vérification des mises à jour..."
    echo

    ./update.sh || true

fi

# --------------------------------------------------------------------------
# Lancement
# --------------------------------------------------------------------------

echo
echo "[INFO] Lancement de ComfyUI..."
echo

exec ./launch.sh --tmux