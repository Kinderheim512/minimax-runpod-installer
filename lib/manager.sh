#!/usr/bin/env bash
# lib/manager.sh — installation / mise à jour de ComfyUI-Manager.

install_or_update_manager() {
  log_step "ComfyUI-Manager"

  local nodes_dir="${INSTALL_DIR}/custom_nodes"
  local target="${nodes_dir}/ComfyUI-Manager"
  mkdir -p "$nodes_dir"

  if [[ -d "${target}/.git" ]]; then
    log_info "ComfyUI-Manager déjà présent, mise à jour..."
    local dirty
    dirty="$(git -C "$target" status --porcelain 2>/dev/null || true)"
    if [[ -n "$dirty" ]]; then
      log_warn "Modifications locales détectées dans ComfyUI-Manager, mise à jour sautée."
    else
      retry "$DOWNLOAD_MAX_RETRIES" git -C "$target" pull --ff-only >>"$LOG_FILE" 2>&1
      log_ok "ComfyUI-Manager mis à jour."
    fi
  else
    log_info "Clonage de ComfyUI-Manager..."
    retry "$DOWNLOAD_MAX_RETRIES" git clone "$COMFYUI_MANAGER_REPO" "$target" >>"$LOG_FILE" 2>&1
    log_ok "ComfyUI-Manager installé."
  fi

  if [[ -f "${target}/requirements.txt" ]]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    pip install -r "${target}/requirements.txt" --quiet
    deactivate
  fi
}
