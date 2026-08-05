#!/usr/bin/env bash
# uninstall.sh — désinstalle proprement ComfyUI et les dépendances installées
# par ce projet. Ne touche jamais aux paquets système (git, aria2, ffmpeg...),
# qui peuvent servir à d'autres usages sur le pod.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/uninstall.log"

for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES="true" ;;
  esac
done

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"

if [[ ! -d "$INSTALL_DIR" ]]; then
  log_info "${INSTALL_DIR} n'existe pas, rien à désinstaller."
  exit 0
fi

log_step "Désinstallation"
log_warn "Ceci va supprimer : ${INSTALL_DIR} (ComfyUI, venv, ComfyUI-Manager, nœuds custom)."

if ! confirm "Confirmer la suppression de ${INSTALL_DIR} ?"; then
  log_info "Désinstallation annulée."
  exit 0
fi

models_dir="${INSTALL_DIR}/models"
keep_models="false"
if [[ -d "$models_dir" ]]; then
  size="$(du -sh "$models_dir" 2>/dev/null | cut -f1)"
  if ! confirm "Supprimer aussi les modèles téléchargés (${models_dir}, ${size:-taille inconnue}) ?"; then
    keep_models="true"
  fi
fi

if [[ "$keep_models" == "true" ]]; then
  tmp_backup="$(mktemp -d)/models"
  log_info "Sauvegarde temporaire des modèles vers ${tmp_backup}..."
  mv "$models_dir" "$tmp_backup"
  rm -rf "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  mv "$tmp_backup" "$models_dir"
  log_ok "ComfyUI supprimé, modèles conservés dans ${models_dir}."
else
  rm -rf "$INSTALL_DIR"
  log_ok "${INSTALL_DIR} entièrement supprimé (y compris les modèles)."
fi

log_info "Paquets système (git, aria2, ffmpeg, python3...) conservés — non gérés par ce projet."
log_ok "Désinstallation terminée."
