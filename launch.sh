#!/usr/bin/env bash
# launch.sh — démarre ComfyUI avec les optimisations calculées pour le GPU.

if curl -fs http://127.0.0.1:8188 >/dev/null 2>&1; then

    echo
    echo "[INFO] ComfyUI est déjà lancé."
    echo

    exit 0

fi
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/launch.log"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  log_error "Environnement virtuel introuvable (${VENV_DIR}). Lancez d'abord bash install.sh."
  exit 1
fi
if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
  log_error "ComfyUI introuvable dans ${INSTALL_DIR}. Lancez d'abord bash install.sh."
  exit 1
fi

MINIMAX_ENV_VARS=()
MINIMAX_LAUNCH_FLAGS=()
flags_file="${INSTALL_DIR}/user/.minimax_launch_flags"
if [[ -f "$flags_file" ]]; then
  # shellcheck disable=SC1091
  source "$flags_file"
else
  log_warn "Aucun fichier d'optimisation trouvé — lancement avec les réglages ComfyUI par défaut."
  log_warn "(Lancez bash install.sh ou bash update.sh pour générer des optimisations adaptées au GPU.)"
fi

for kv in "${MINIMAX_ENV_VARS[@]+"${MINIMAX_ENV_VARS[@]}"}"; do
  [[ -z "$kv" ]] && continue
  export "${kv?}"
done

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

read -r -a extra_args_from_config <<< "${EXTRA_LAUNCH_ARGS}"

log_step "Lancement de ComfyUI"
log_info "Répertoire   : ${INSTALL_DIR}"
log_info "Écoute       : ${COMFYUI_LISTEN}:${COMFYUI_PORT}"
[[ ${#MINIMAX_LAUNCH_FLAGS[@]} -gt 0 ]] && log_info "Flags GPU    : ${MINIMAX_LAUNCH_FLAGS[*]}"
[[ -n "$EXTRA_LAUNCH_ARGS" ]] && log_info "Flags manuels: ${EXTRA_LAUNCH_ARGS}"

if [[ -n "${RUNPOD_POD_ID:-}" ]]; then
  echo ""
  log_ok "URL RunPod (proxy HTTP, une fois le serveur démarré) :"
  echo -e "  ${C_BOLD}https://${RUNPOD_POD_ID}-${COMFYUI_PORT}.proxy.runpod.net${C_RESET}"
  echo "  (assurez-vous que le port ${COMFYUI_PORT} est bien exposé en HTTP dans les réglages du pod)"
else
  echo ""
  log_ok "URL locale (une fois le serveur démarré) :"
  echo -e "  ${C_BOLD}http://<ip-publique-du-pod>:${COMFYUI_PORT}${C_RESET}"
fi
echo ""

cd "$INSTALL_DIR"
exec python main.py \
  --listen "$COMFYUI_LISTEN" \
  --port "$COMFYUI_PORT" \
  "${MINIMAX_LAUNCH_FLAGS[@]+"${MINIMAX_LAUNCH_FLAGS[@]}"}" \
  "${extra_args_from_config[@]+"${extra_args_from_config[@]}"}"