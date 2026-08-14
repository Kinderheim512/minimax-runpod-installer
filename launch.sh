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
# Défini avant de sourcer lib/utils.sh (qui ne fixe ce chemin que si vide)
# pour que TOUS les logs de ce script, dès la toute première ligne,
# atterrissent dans launch.log — comportement inchangé par rapport à avant.
LOG_FILE="${PROJECT_ROOT}/logs/launch.log"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"

# Détection d'un process ComfyUI "fantôme" ------------------------------
# But : distinguer trois situations avant de lancer, plutôt qu'une seule
# ("répond sur le port" / "ne répond pas") :
#   1. Un serveur tourne ET répond déjà sur COMFYUI_PORT -> rien à faire.
#   2. Un process main.py de CETTE installation tourne mais ne répond pas
#      encore (démarrage en cours : chargement modèles, ~30-60s) OU plus du
#      tout (planté/bloqué, VRAM potentiellement toujours occupée) -> ne
#      JAMAIS lancer une seconde instance par-dessus (elle se battrait pour
#      le même port et la même VRAM) : on prévient et on laisse la main.
#   3. Rien ne tourne -> lancement normal.
# `main.py` est lancé avec un chemin RELATIF (`cd "$INSTALL_DIR"; exec python
# main.py`, cf. plus bas) : la ligne de commande ne contient donc jamais
# INSTALL_DIR en clair, et un simple `pgrep -f ".../main.py"` ne matcherait
# jamais rien. On identifie donc chaque candidat par sa ligne de commande
# (contient "main.py"), puis on vérifie son répertoire de travail réel via
# /proc/<pid>/cwd — c'est ça qui garantit qu'on cible bien CETTE installation
# ComfyUI (INSTALL_DIR), pas un autre process python du système qui aurait
# aussi "main.py" dans sa ligne de commande.
find_comfyui_pid() {
  require_cmd pgrep || return 0
  local target_dir pid cwd
  target_dir="$(readlink -f "$INSTALL_DIR" 2>/dev/null)"
  [[ -n "$target_dir" ]] || return 0
  for pid in $(pgrep -f 'python[3]?[^&]*main\.py' 2>/dev/null); do
    cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null)"
    [[ -n "$cwd" && "$cwd" == "$target_dir" ]] && echo "$pid"
  done
  # Sous `set -e` (utilisé par ce script), une fonction appelée comme
  # `var="$(find_comfyui_pid)"` fait avorter tout le script si son code de
  # sortie est non-nul — or "rien trouvé" (aucune ligne imprimée par le
  # `for` ci-dessus) est un résultat NORMAL, pas une erreur : sans ce
  # `return 0` explicite, le statut de sortie hérite de celui du dernier
  # `[[ ... ]] && echo` évalué (1 s'il n'y avait pas de match), ce qui
  # ferait planter stop_comfyui() en plein milieu au 2e appel de
  # vérification post-kill (bug repéré en testant cette correction).
  return 0
}

stop_comfyui() {
  local pids
  pids="$(find_comfyui_pid)"
  if [[ -z "$pids" ]]; then
    log_info "Aucun process ComfyUI (${INSTALL_DIR}/main.py) trouvé — rien à arrêter."
    return 0
  fi
  log_info "Arrêt de ComfyUI (PID : ${pids//$'\n'/, })..."
  # shellcheck disable=SC2086  # plusieurs PID possibles sur des lignes séparées, split volontaire
  kill $pids 2>/dev/null || true
  sleep 2
  pids="$(find_comfyui_pid)"
  if [[ -n "$pids" ]]; then
    log_warn "Toujours actif après SIGTERM (PID : ${pids//$'\n'/, }) — arrêt forcé (SIGKILL)."
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
  log_ok "ComfyUI arrêté — la VRAM qu'il occupait devrait être libérée sous peu."
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_comfyui
  exit 0
fi

attach_tmux_if_interactive() {
  # Point d'attache unique pour la session tmux : s'exécute exactement comme
  # avant quand un vrai terminal est présent (stdin ET stdout), et se
  # contente d'indiquer comment se rattacher plus tard sinon — au lieu de
  # laisser échouer un "exec tmux attach-session" sans TTY (cas d'un
  # bootstrap.sh lancé de façon non interactive : curl | bash, Start Command
  # RunPod, etc.). ComfyUI, lui, tourne déjà dans la session détachée à ce
  # stade et n'est pas affecté par ce choix.
  #
  # CAS IMBRIQUÉ — si ce script tourne DANS un client tmux déjà attaché à une
  # AUTRE session (ex. le terminal web RunPod ouvre lui-même une session par
  # défaut), "$TMUX" est déjà présent dans l'environnement : un
  # "tmux attach-session" classique refuse alors de s'imbriquer
  # ("sessions should be nested with care, unset $TMUX to force"), échoue, et
  # à cause du "exec" ce script se termine aussitôt sans jamais atteindre la
  # session "minimax" — ComfyUI tourne bien dedans, mais rien ne l'affiche,
  # ce qui donne l'impression que "tmux ne démarre pas". La bonne commande
  # dans ce cas est "tmux switch-client" : elle change simplement la session
  # affichée par le client tmux courant, sans l'imbriquer.
  if [[ -n "${TMUX:-}" ]]; then
    exec tmux switch-client -t "$TMUX_SESSION_NAME"
  fi

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

if curl -fs "http://127.0.0.1:${COMFYUI_PORT}" >/dev/null 2>&1; then
    echo
    log_ok "ComfyUI est déjà lancé et répond sur le port ${COMFYUI_PORT}."
    echo
    exit 0
fi

existing_pid="$(find_comfyui_pid)"
if [[ -n "$existing_pid" ]]; then
  echo
  log_warn "Un process ComfyUI (PID : ${existing_pid//$'\n'/, }) tourne déjà mais ne répond pas (encore) sur le port ${COMFYUI_PORT}."
  log_warn "S'il vient d'être lancé : patientez le temps du chargement des modèles (30-60s selon le GPU) puis réessayez."
  log_warn "S'il est bloqué/planté (VRAM potentiellement toujours occupée) : bash launch.sh --stop, puis relancez."
  echo
  exit 1
fi

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
