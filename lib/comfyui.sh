#!/usr/bin/env bash
# lib/comfyui.sh — installation et mise à jour du dépôt ComfyUI.
#
# Le support natif de MiniMax H3 (nœuds MiniMaxH3ImageToVideo /
# MiniMaxH3ReferenceToVideo) est arrivé dans ComfyUI >= 0.30.0. On suit donc
# toujours la branche par défaut du dépôt pour être sûr de l'avoir, plutôt
# que de figer un tag qui deviendrait vite obsolète.

clone_or_update_comfyui() {
  log_step "Installation / mise à jour de ComfyUI"

  mkdir -p "$(dirname "$INSTALL_DIR")"

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log_info "ComfyUI déjà cloné dans ${INSTALL_DIR}, mise à jour..."
    update_comfyui
  elif [[ -e "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    log_error "${INSTALL_DIR} existe déjà, n'est pas vide, et n'est pas un dépôt git ComfyUI."
    log_error "Choisissez un autre INSTALL_DIR (dans config.env ou en variable d'environnement), ou videz ce dossier avant de relancer."
    exit 1
  else
    log_info "Clonage de ${COMFYUI_REPO} dans ${INSTALL_DIR}"
    retry "$DOWNLOAD_MAX_RETRIES" git clone --branch "$COMFYUI_BRANCH" "$COMFYUI_REPO" "$INSTALL_DIR" >>"$LOG_FILE" 2>&1
    log_ok "ComfyUI cloné."
  fi

  print_comfyui_version
}

update_comfyui() {
  local dirty
  dirty="$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null || true)"
  if [[ -n "$dirty" ]]; then
    log_warn "Des modifications locales existent dans ${INSTALL_DIR} — mise à jour git sautée pour ne rien écraser."
    log_warn "Lancez 'git stash' manuellement dans ce dossier puis relancez update.sh si vous voulez forcer la mise à jour."
    return 0
  fi
  {
    retry "$DOWNLOAD_MAX_RETRIES" git -C "$INSTALL_DIR" fetch --tags origin
    git -C "$INSTALL_DIR" checkout "$COMFYUI_BRANCH"
    retry "$DOWNLOAD_MAX_RETRIES" git -C "$INSTALL_DIR" pull --ff-only origin "$COMFYUI_BRANCH"
  } >>"$LOG_FILE" 2>&1
  log_ok "ComfyUI mis à jour."
}

print_comfyui_version() {
  local rev version_str
  rev="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo inconnu)"
  version_str="$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || echo inconnu)"
  log_info "ComfyUI — commit ${rev} (${version_str})"

  # Comparaison best-effort avec MIN_COMFYUI_VERSION (avertissement seulement,
  # ComfyUI ne tague pas systématiquement chaque commit de master).
  if [[ "$version_str" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
    local maj="${BASH_REMATCH[1]}" min="${BASH_REMATCH[2]}"
    IFS='.' read -r req_maj req_min _ <<< "$MIN_COMFYUI_VERSION"
    if (( maj < req_maj || (maj == req_maj && min < req_min) )); then
      log_warn "Version ComfyUI (${version_str}) potentiellement antérieure à ${MIN_COMFYUI_VERSION} requis pour MiniMax H3 natif."
    else
      log_ok "Version ComfyUI compatible MiniMax H3 natif (>= ${MIN_COMFYUI_VERSION})."
    fi
  else
    log_info "Impossible de comparer précisément à ${MIN_COMFYUI_VERSION} (pas de tag exact) — branche ${COMFYUI_BRANCH} à jour, ce qui inclut le support H3 le plus récent."
  fi
}
