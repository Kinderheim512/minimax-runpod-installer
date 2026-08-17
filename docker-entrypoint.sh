#!/usr/bin/env bash
# docker-entrypoint.sh — point d'entrée du conteneur pour l'image Docker
# pré-installée (voir Dockerfile). Tout ce qui NE dépendait PAS du GPU a
# déjà été fait à la CONSTRUCTION de l'image (docker-build-steps-heavy.sh puis docker-build-steps-light.sh) ; ce
# script fait le reste, au DÉMARRAGE du conteneur, une fois le GPU alloué à
# ce pod réellement visible via nvidia-smi :
#   1. Installe UNIQUEMENT PyTorch (install_pytorch, lib/python.sh) — même
#      table PYTORCH_BUILD_TABLE, même fonction que install.sh/update.sh,
#      rien de dupliqué ici.
#   2. Lance install.sh normalement pour la suite (poids H3, workflows,
#      presets, stockage perso...) : les étapes déjà réalisées à la
#      construction de l'image (voir docker-build-steps-heavy.sh/docker-build-steps-light.sh) sont marquées
#      dans le state file baked dans l'image et donc sautées directement par
#      run_step/step_done (lib/utils.sh) — install.sh ne rejoue que ce qui
#      reste réellement à faire.
#   3. Lance ComfyUI (launch.sh).
#
# Idempotent : si le conteneur redémarre (arrêt/relance du même conteneur,
# pas un nouveau pod), install_pytorch() détecte que le build déjà installé
# correspond au GPU détecté et ne réinstalle rien ; install.sh, via son
# propre state file, ne rejoue que les étapes pas encore marquées faites.
#
# "$@" (arguments passés à `docker run`) est transmis tel quel à install.sh,
# même principe que bootstrap.sh pour l'usage bash classique (ex:
# `docker run ... image --tier=balanced --yes`).

set -Eeuo pipefail
PROJECT_ROOT="/opt/minimax-runpod-installer"
cd "$PROJECT_ROOT"

LOG_FILE="${PROJECT_ROOT}/logs/docker-entrypoint.log"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"
enable_error_trap
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/python.sh"

echo -e "${C_BOLD}${C_CYAN}"
echo "  ┌────────────────────────────────────────────────────┐"
echo "  │  MiniMax H3 — image Docker pré-installée            │"
echo "  └────────────────────────────────────────────────────┘"
echo -e "${C_RESET}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  log_error "Venv introuvable dans ${VENV_DIR} — cette image a-t-elle bien été construite via le Dockerfile de ce projet (docker-build-steps-heavy.sh) ?"
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  log_error "nvidia-smi introuvable dans le conteneur — ce pod a-t-il bien un GPU NVIDIA attaché (runtime container NVIDIA) ?"
  exit 1
fi

log_step "Installation de PyTorch pour le GPU de ce conteneur"
install_pytorch

log_step "Suite de l'installation (poids H3, workflows, stockage perso...)"
./install.sh "$@"

log_step "Lancement de ComfyUI"
exec ./launch.sh
