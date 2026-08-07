#!/usr/bin/env bash
# update.sh — met à jour ComfyUI, ComfyUI-Manager, les nœuds custom optionnels
# et les dépendances Python. Ne re-télécharge pas les modèles déjà présents ;
# utilisez models.sh (ou install.sh --only-models) si de nouveaux fichiers
# de poids sont sortis pour un palier que vous voulez ajouter.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # lu par lib/utils.sh une fois sourcé (log_*())
LOG_FILE="${PROJECT_ROOT}/logs/update.log"

for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES="true" ;;
  esac
done

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"
enable_error_trap
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/gpu.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/python.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/comfyui.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/manager.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/nodes.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/optimization.sh"

if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  log_error "${INSTALL_DIR} n'existe pas encore — lancez d'abord install.sh."
  exit 1
fi

log_step "Mise à jour du projet MiniMax H3 / ComfyUI"

clone_or_update_comfyui
install_comfyui_requirements
install_extra_requirements
install_or_update_manager
install_optional_nodes

detect_gpu
compute_optimization_flags

log_ok "Mise à jour terminée."
