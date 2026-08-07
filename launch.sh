#!/usr/bin/env bash
# launch.sh — démarre ComfyUI avec les optimisations calculées pour le GPU.
#
# Usage :
#   bash launch.sh          -> lance ComfyUI directement (premier plan, bloquant)
#   bash launch.sh --tmux   -> lance ComfyUI dans une session tmux persistante
#                              nommée "minimax" : la crée si elle n'existe pas
#                              encore (et lance ComfyUI dedans), ou s'y attache
#                              simplement si elle existe déjà. Ne duplique
#                              jamais la logique de lancement ci-dessous : le
#                              mode tmux ne fait qu'appeler ce même script à
#                              l'intérieur de la session.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMUX_SESSION_NAME="minimax"

attach_tmux_if_interactive() {
  # Point d'attache unique pour la session tmux : s'exécute exactement comme
  # avant quand un vrai terminal est présent (stdin ET stdout), et se
  # contente d'indiquer comment se rattacher plus tard sinon — au lieu de
  # laisser échouer un "exec tmux attach-session" sans TTY (cas d'un
  # bootstrap.sh lancé de façon non interactive : curl | bash, Start Command
  # RunPod, etc.). ComfyUI, lui, tourne déjà dans la session détachée à ce
  # stade et n'est pas affecté par ce choix.
  if [[ -t 0 && -t 1 ]]; then
    exec tmux attach-session -t "$TMUX_SESSION_NAME"
  fi

  echo "[INFO] Pas de terminal interactif détecté — la session tmux '${TMUX_SESSION_NAME}' continue de tourner en arrière-plan."
  echo "[INFO] Pour vous y rattacher plus tard : tmux attach -t ${TMUX_SESSION_NAME}"
  # ComfyUI tourne déjà dans la session tmux détachée : on doit s'arrêter ici,
  # sinon le flux repasserait dans launch_in_tmux() puis dans le corps
  # principal de ce script et lancerait un second ComfyUI hors tmux.
  exit 0
}

launch_in_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "[ERREUR] tmux est introuvable (il devrait pourtant être installé automatiquement)." >&2
    exit 1
  fi

  if tmux has-session -t "$TMUX_SESSION_NAME" 2>/dev/null; then
    echo "[INFO] Session tmux '${TMUX_SESSION_NAME}' déjà existante — attache..."
    attach_tmux_if_interactive
  fi

  echo "[INFO] Lancement de ComfyUI dans tmux..."
  echo "[INFO] Création de la session tmux '${TMUX_SESSION_NAME}'..."
  # La session relance simplement ce même script (sans --tmux) : c'est lui
  # qui sait déjà activer le bon venv et détecter une instance déjà en
  # cours (message "ComfyUI est déjà lancé." ci-dessous). Aucune logique de
  # lancement n'est dupliquée ici. "; exec bash" garde la session ouverte
  # après coup (ComfyUI déjà lancé, arrêt, etc.) pour pouvoir s'y attacher.
  local session_cmd="bash \"${PROJECT_ROOT}/launch.sh\"; exec bash"
  tmux new-session -d -s "$TMUX_SESSION_NAME" "$session_cmd"
  attach_tmux_if_interactive
}

if [[ "${1:-}" == "--tmux" ]]; then
  launch_in_tmux
fi

if curl -fs "http://127.0.0.1:8188" >/dev/null 2>&1; then

    echo
    echo "[INFO] ComfyUI est déjà lancé."
    echo

    exit 0

fi

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
  # shellcheck disable=SC1090,SC1091  # chemin dynamique, généré à l'exécution
  # par compute_optimization_flags (lib/optimization.sh) : rien à suivre au lint.
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
