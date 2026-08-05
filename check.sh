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

verify_installation
exit_code=$?
print_summary
exit "$exit_code"
