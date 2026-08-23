#!/usr/bin/env bash
# lib/manager.sh — installation / mise à jour de ComfyUI-Manager.

install_or_update_manager() {
  log_step "$(t manager_step)"

  local nodes_dir="${INSTALL_DIR}/custom_nodes"
  local target="${nodes_dir}/ComfyUI-Manager"
  mkdir -p "$nodes_dir"

  if [[ -d "${target}/.git" ]]; then
    log_info "$(t manager_updating)"
    local dirty
    dirty="$(git -C "$target" status --porcelain 2>/dev/null || true)"
    if [[ -n "$dirty" ]]; then
      log_warn "$(t manager_local_changes)"
    else
      retry "$DOWNLOAD_MAX_RETRIES" git -C "$target" pull --ff-only >>"$LOG_FILE" 2>&1
      log_ok "$(t manager_updated)"
    fi
  else
    log_info "$(t manager_cloning)"
    retry "$DOWNLOAD_MAX_RETRIES" git clone "$COMFYUI_MANAGER_REPO" "$target" >>"$LOG_FILE" 2>&1
    log_ok "$(t manager_installed)"
  fi

  if [[ -f "${target}/requirements.txt" ]]; then
    # cf. lib/python.sh::pip_install_requirements — filtre torch à la
    # construction de l'image Docker (DOCKER_BUILD_NO_TORCH), transparent
    # sinon (install.sh/update.sh classiques).
    pip_install_requirements "${target}/requirements.txt"
  fi
}
