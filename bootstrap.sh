#!/usr/bin/env bash
# ==============================================================================
# bootstrap.sh
# Bootstrap automatique MiniMax H3 + ComfyUI pour RunPod
# ==============================================================================

set -Eeuo pipefail

REPO_URL="https://github.com/Kinderheim512/minimax-runpod-installer.git"
INSTALL_DIR="/workspace/minimax-runpod-installer"
COMFY_DIR="/workspace/ComfyUI"

# i18n : lib/i18n.sh n'existe pas encore avant le premier clone (dépôt pas
# encore présent) — on tente de la sourcer une première fois (cas "dépôt déjà
# présent"), sinon on retente juste après le clone, avant tout message
# utilisateur qui en dépend. `t()` retombe sur un writer minimal tant que ni
# l'un ni l'autre n'a réussi (premier message "Bootstrap..." ci-dessous),
# pour ne jamais planter le script si les deux tentatives échouent.
t() { local key="$1"; shift || true; printf '%s' "$key"; }
techo() { local key="$1"; shift || true; echo "$key"; }
# shellcheck disable=SC1091
[[ -f "${INSTALL_DIR}/lib/i18n.sh" ]] && source "${INSTALL_DIR}/lib/i18n.sh"

echo
echo "=============================================================="
techo bootstrap_title
echo "=============================================================="
echo

# --------------------------------------------------------------------------
# Vérification Git
# --------------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "[ERROR] $(t bootstrap_no_git)"
    exit 1
fi

# --------------------------------------------------------------------------
# Clone ou mise à jour
# --------------------------------------------------------------------------

if [[ ! -d "$INSTALL_DIR/.git" ]]; then

    echo "[INFO] $(t bootstrap_cloning)"

    git clone "$REPO_URL" "$INSTALL_DIR"

    # shellcheck disable=SC1091
    [[ -f "${INSTALL_DIR}/lib/i18n.sh" ]] && source "${INSTALL_DIR}/lib/i18n.sh"

else

    echo "[INFO] $(t bootstrap_already_present)"

    cd "$INSTALL_DIR"

    echo "[INFO] $(t bootstrap_updating)"

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
    echo "[WARNING] $(t bootstrap_no_hf_token)"
    echo
    techo bootstrap_no_hf_token_detail
    echo
    techo bootstrap_no_hf_token_fix
    echo

fi

# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------
# "$@" est transmis à install.sh et update.sh : c'est la seule façon pour un
# appel du type `bash bootstrap.sh --tier=balanced` de faire parvenir ses
# options jusqu'au script qui les parse réellement (install.sh). update.sh
# ignore silencieusement toute option qu'il ne connaît pas (seul --yes/-y y
# est reconnu), donc lui transmettre "$@" est sans danger et lui permet de
# recevoir --yes/-y de la même façon.

if [[ ! -f "$COMFY_DIR/main.py" ]]; then

    echo
    echo "[INFO] $(t bootstrap_first_install)"
    echo

    ./install.sh "$@"

else

    echo
    echo "[INFO] $(t bootstrap_already_installed)"
    echo "[INFO] $(t bootstrap_checking_updates)"
    echo

    ./update.sh "$@" || true

fi

# --------------------------------------------------------------------------
# Lancement
# --------------------------------------------------------------------------

echo
echo "[INFO] $(t bootstrap_launching)"
echo

exec ./launch.sh --tmux
