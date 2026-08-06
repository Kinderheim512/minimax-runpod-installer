#!/usr/bin/env bash
# check.sh — vérifie l'état de l'installation sans rien modifier.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/install.log"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/gpu.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/models.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/verify.sh"

if require_cmd nvidia-smi && nvidia-smi >/dev/null 2>&1; then
  detect_gpu
fi

exit_code=0
verify_installation || exit_code=$?

if [[ "${INSTALL_SPECTRUM:-true}" == "true" ]]; then
  if [[ -d "${INSTALL_DIR}/custom_nodes/ComfyUI-Spectrum-MiniMax-H3" ]]; then
    log_ok "Spectrum MiniMax H3 : installé."
  else
    log_warn "Spectrum MiniMax H3 : non installé (optionnel, INSTALL_SPECTRUM=true) — bash install.sh"
  fi
fi

print_summary
exit "$exit_code"
