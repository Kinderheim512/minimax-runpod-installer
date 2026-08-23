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
#
# MINIMAX_DEBUG_SLEEP=true (variable d'environnement du pod) : échappatoire
# de secours pour obtenir un shell stable en cas de boucle de redémarrage
# (ce script qui plante, RunPod qui relance le conteneur en boucle, jamais
# de fenêtre pour s'y connecter). Vérifiée EN TOUT PREMIER, avant même
# nvidia-smi/PyTorch/install.sh : si activée, le conteneur reste vivant
# indéfiniment sans rien installer ni lancer, pour laisser le temps de se
# connecter (terminal web RunPod ou SSH) et diagnostiquer/corriger à la
# main. Volontairement une variable d'environnement plutôt qu'un override de
# la commande de démarrage du pod (ex: "sleep infinity" côté RunPod) : ce
# dernier n'est PAS garanti remplacer l'ENTRYPOINT du conteneur selon les
# interfaces RunPod — il peut au contraire être ajouté comme argument à CE
# script, qui les transmettrait tel quel à install.sh et ferait échouer sur
# une option inconnue (vécu). Une variable d'environnement, elle, n'a pas
# cette ambiguïté : la section "Environment Variables" de RunPod est fiable.
PROJECT_ROOT="/opt/minimax-runpod-installer"
# shellcheck disable=SC1091
[[ -f "${PROJECT_ROOT}/config.env" ]] && source "${PROJECT_ROOT}/config.env"
# Repli minimal si lib/i18n.sh est exceptionnellement absent de l'image
# (jamais le cas normalement — voir docker-build-steps-light.sh) : évite un
# "command not found" sur t()/techo() plutôt qu'un vrai message traduit.
t() { local key="$1"; shift || true; printf '%s' "$key"; }
techo() { local key="$1"; shift || true; echo "$key"; }
# shellcheck disable=SC1091
[[ -f "${PROJECT_ROOT}/lib/i18n.sh" ]] && source "${PROJECT_ROOT}/lib/i18n.sh"

if [[ "${MINIMAX_DEBUG_SLEEP:-false}" == "true" ]]; then
  techo entrypoint_debug_sleep
  exec sleep infinity
fi

set -Eeuo pipefail
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
printf '  │  %-50s│\n' "$(t entrypoint_banner_title)"
echo "  └────────────────────────────────────────────────────┘"
echo -e "${C_RESET}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  log_error "$(t entrypoint_venv_missing "$VENV_DIR")"
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  log_error "$(t entrypoint_no_nvidia_smi)"
  exit 1
fi

log_step "$(t entrypoint_installing_pytorch)"
install_pytorch

log_step "$(t entrypoint_rest_of_install)"
./install.sh "$@"

log_step "$(t entrypoint_launching)"
exec ./launch.sh
