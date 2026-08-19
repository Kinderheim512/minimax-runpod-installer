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
source "${PROJECT_ROOT}/lib/system.sh"
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
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/lora_auto.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/huggingface.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/personal_storage.sh"

if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  log_error "${INSTALL_DIR} n'existe pas encore — lancez d'abord install.sh."
  exit 1
fi

log_step "Mise à jour du projet MiniMax H3 / ComfyUI"

# Garantit python3-dev/ninja-build/build-essential (et le reste des paquets
# système) AVANT tout ce qui en dépend plus bas (install_comfyui_requirements,
# install_sageattention) — même étape que install.sh (run_step
# "system_packages"), mais appelée ici SANS run_step : bootstrap.sh route
# tout pod déjà installé (main.py présent) vers CE script, jamais
# install.sh (voir bootstrap.sh) — c'est donc le seul endroit qui peut
# réparer un pod existant dont les paquets système datent d'avant un fix
# comme l'ajout de python3-dev. Idempotent et rapide si déjà en place
# (dpkg -s par paquet, cf. lib/system.sh) : n'ajoute pas de délai notable
# aux mises à jour normales.
install_system_packages

clone_or_update_comfyui
install_comfyui_requirements
install_extra_requirements

detect_gpu
detect_system_ram

install_sageattention
install_or_update_manager
install_optional_nodes

# Exception délibérée à la règle générale "update.sh ne touche pas aux
# modèles" (cf. en-tête de ce fichier) : le Turbo LoRA est un fichier unique
# et petit, idempotent nativement (install_lora.sh saute le téléchargement
# s'il est déjà présent), pas le système de poids H3 multi-Go que cette
# règle vise à protéger. Nécessaire ici car bootstrap.sh route tout pod déjà
# installé (main.py présent) vers update.sh, jamais install.sh — sans cet
# appel, un pod redémarré avec un volume réseau où ce LoRA manquerait
# (ancien volume antérieur à cette fonctionnalité, suppression manuelle...)
# ne le récupérerait jamais automatiquement. Même raisonnement pour le
# custom node Turbo ci-dessous : install_turbo_node() est un simple clone-
# si-absent (jamais de git pull automatique, voir lib/lora_auto.sh), donc
# sans risque de mise à jour intempestive à chaque redémarrage de pod.
install_turbo_node
install_turbo_lora

compute_optimization_flags

# Sauvegarde best-effort du stockage perso (LoRAs/presets/outputs) en fin de
# mise à jour — no-op silencieux si PERSONAL_STORAGE_HF_REPO est vide (voir
# config.env). Pour un push manuel à tout moment (typiquement juste avant de
# terminate un pod, sans attendre un update.sh), voir : bash sync_push.sh
sync_personal_storage_push

log_ok "Mise à jour terminée."
