#!/usr/bin/env bash
# sync_push.sh — envoie manuellement LoRAs/presets/outputs perso vers le
# coffre Hugging Face (PERSONAL_STORAGE_HF_REPO, config.env). À lancer
# typiquement juste avant de terminate un pod RunPod, pour ne rien perdre de
# ce qui n'est pas déjà sur le Network Volume (payant, lié à un seul
# datacenter — voir la section correspondante du README). update.sh appelle
# déjà cette même synchronisation automatiquement en fin de mise à jour ;
# ce script permet de la déclencher à tout autre moment, sans attendre un
# update.sh complet.
#
# Usage : bash sync_push.sh

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # lu par lib/utils.sh une fois sourcé (log_*())
LOG_FILE="${PROJECT_ROOT}/logs/sync_push.log"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"
enable_error_trap
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/huggingface.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/personal_storage.sh"

if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
  log_error "$(t syncpush_venv_missing "$VENV_DIR")"
  exit 1
fi

sync_personal_storage_push
