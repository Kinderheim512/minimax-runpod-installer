#!/usr/bin/env bash
# uninstall.sh — désinstalle proprement ComfyUI et les dépendances installées
# par ce projet. Ne touche jamais aux paquets système (git, aria2, ffmpeg...),
# qui peuvent servir à d'autres usages sur le pod.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # lu par lib/utils.sh une fois sourcé (log_*())
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
  log_info "$(t uninstall_nothing "$INSTALL_DIR")"
  exit 0
fi

log_step "$(t uninstall_step)"
log_warn "$(t uninstall_warn "$INSTALL_DIR")"

if ! confirm "$(t uninstall_confirm "$INSTALL_DIR")"; then
  log_info "$(t uninstall_cancelled)"
  exit 0
fi

models_dir="${INSTALL_DIR}/models"
keep_models="false"
if [[ -d "$models_dir" ]]; then
  size="$(du -sh "$models_dir" 2>/dev/null | cut -f1)"
  if ! confirm "$(t uninstall_confirm_models "$models_dir" "${size:-$(t uninstall_size_unknown)}")"; then
    keep_models="true"
  fi
fi

if [[ "$keep_models" == "true" ]]; then
  tmp_backup="$(mktemp -d)/models"
  log_info "$(t uninstall_backing_up_models "$tmp_backup")"
  mv "$models_dir" "$tmp_backup"
  rm -rf "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  mv "$tmp_backup" "$models_dir"
  log_ok "$(t uninstall_kept_models "$models_dir")"
else
  rm -rf "$INSTALL_DIR"
  log_ok "$(t uninstall_removed_all "$INSTALL_DIR")"
fi

log_info "$(t uninstall_system_packages_kept)"
log_ok "$(t uninstall_done)"
