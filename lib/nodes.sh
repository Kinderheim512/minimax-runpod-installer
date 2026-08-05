#!/usr/bin/env bash
# lib/nodes.sh — nœuds custom optionnels.
#
# Important : aucun custom node n'est requis pour faire fonctionner MiniMax
# H3 dans ComfyUI. Le support (nœuds MiniMaxH3ImageToVideo et
# MiniMaxH3ReferenceToVideo) est natif depuis ComfyUI >= 0.30.0 — voir
# lib/comfyui.sh. Cette étape installe uniquement des extensions de confort
# listées dans OPTIONAL_NODE_REPOS (config.env), et ne bloque jamais
# l'installation en cas d'échec d'un paquet optionnel.

install_optional_nodes() {
  log_step "Nœuds custom optionnels"

  if [[ "$INSTALL_OPTIONAL_NODES" != "true" ]]; then
    log_info "INSTALL_OPTIONAL_NODES=false, étape sautée."
    return 0
  fi

  if [[ ${#OPTIONAL_NODE_REPOS[@]} -eq 0 ]]; then
    log_info "Aucun nœud optionnel configuré."
    return 0
  fi

  local nodes_dir="${INSTALL_DIR}/custom_nodes"
  mkdir -p "$nodes_dir"

  for repo_url in "${OPTIONAL_NODE_REPOS[@]}"; do
    local name; name="$(basename "$repo_url" .git)"
    local target="${nodes_dir}/${name}"

    if [[ -d "${target}/.git" ]]; then
      log_info "${name} déjà présent — mise à jour..."
      local dirty
      dirty="$(git -C "$target" status --porcelain 2>/dev/null || true)"
      if [[ -n "$dirty" ]]; then
        log_warn "${name} a des modifications locales, mise à jour sautée."
        continue
      fi
      if ! git -C "$target" pull --ff-only >>"$LOG_FILE" 2>&1; then
        log_warn "Échec de mise à jour de ${name} (non bloquant)."
        continue
      fi
    else
      log_info "Installation de ${name}..."
      if ! git clone "$repo_url" "$target" >>"$LOG_FILE" 2>&1; then
        log_warn "Échec du clonage de ${name} (non bloquant, on continue)."
        continue
      fi
    fi

    if [[ -f "${target}/requirements.txt" ]]; then
      # shellcheck disable=SC1091
      source "${VENV_DIR}/bin/activate"
      pip install -r "${target}/requirements.txt" --quiet >>"$LOG_FILE" 2>&1 || \
        log_warn "Dépendances de ${name} partiellement installées (non bloquant)."
      deactivate
    fi
    log_ok "${name} prêt."
  done

  log_info "D'autres nœuds peuvent être ajoutés à tout moment via ComfyUI-Manager, directement dans l'interface web."
}
