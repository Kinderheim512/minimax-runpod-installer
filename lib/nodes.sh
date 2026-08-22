#!/usr/bin/env bash
# lib/nodes.sh — nœuds custom optionnels.
#
# Important : aucun custom node n'est requis pour faire fonctionner MiniMax
# H3 dans ComfyUI. Le support (nœuds MiniMaxH3ImageToVideo et
# MiniMaxH3ReferenceToVideo) est natif depuis ComfyUI >= 0.30.0 — voir
# lib/comfyui.sh. Cette étape installe uniquement des extensions de confort :
#   - OPTIONAL_NODE_REPOS         : dépôts avec requirements.txt éventuel,
#                                   installé automatiquement s'il est présent.
#   - OPTIONAL_NODE_REPOS_NO_PIP  : dépôts volontairement sans dépendance
#                                   Python — requirements.txt n'est jamais
#                                   recherché ni installé pour ces dépôts,
#                                   même s'il en existait un par erreur.
# Les deux tableaux (config.env) partagent exactement la même logique de
# clonage/mise à jour idempotente ; seule la gestion de pip diffère. Cette
# étape ne bloque jamais l'installation en cas d'échec d'un paquet optionnel.

# _clone_or_update_node_repo <repo_url> <allow_pip>
# Logique de clonage/mise à jour partagée par tous les nœuds custom
# optionnels. Idempotent : clone si absent, `git pull --ff-only` si déjà
# présent (mise à jour sautée si des modifications locales sont détectées).
# N'installe requirements.txt que si allow_pip="true". Jamais bloquant : un
# échec logue un avertissement et passe au nœud suivant.
_clone_or_update_node_repo() {
  local repo_url="$1" allow_pip="$2"
  local nodes_dir="${INSTALL_DIR}/custom_nodes"
  local name; name="$(basename "$repo_url" .git)"
  local target="${nodes_dir}/${name}"

  if [[ -d "${target}/.git" ]]; then
    log_info "$(t nodes_updating "$name")"
    local dirty
    dirty="$(git -C "$target" status --porcelain 2>/dev/null || true)"
    if [[ -n "$dirty" ]]; then
      log_warn "$(t nodes_local_changes "$name")"
      return 0
    fi
    if ! git -C "$target" pull --ff-only >>"$LOG_FILE" 2>&1; then
      log_warn "$(t nodes_update_failed "$name")"
      return 0
    fi
  else
    log_info "$(t nodes_installing "$name")"
    if ! git clone "$repo_url" "$target" >>"$LOG_FILE" 2>&1; then
      log_warn "$(t nodes_clone_failed "$name")"
      return 0
    fi
  fi

  if [[ "$allow_pip" == "true" && -f "${target}/requirements.txt" ]]; then
    # cf. lib/python.sh::pip_install_requirements — filtre torch à la
    # construction de l'image Docker (DOCKER_BUILD_NO_TORCH), transparent
    # sinon (install.sh/update.sh classiques).
    pip_install_requirements "${target}/requirements.txt" || \
      log_warn "$(t nodes_deps_partial "$name")"
  fi
  log_ok "$(t nodes_ready "$name")"
}

install_optional_nodes() {
  log_step "$(t nodes_step)"

  if [[ "$INSTALL_OPTIONAL_NODES" != "true" ]]; then
    log_info "$(t nodes_disabled)"
    return 0
  fi

  local total=$(( ${#OPTIONAL_NODE_REPOS[@]} + ${#OPTIONAL_NODE_REPOS_NO_PIP[@]} ))
  if [[ $total -eq 0 ]]; then
    log_info "$(t nodes_none_configured)"
    return 0
  fi

  mkdir -p "${INSTALL_DIR}/custom_nodes"

  local repo_url
  for repo_url in "${OPTIONAL_NODE_REPOS[@]}"; do
    _clone_or_update_node_repo "$repo_url" "true"
  done
  for repo_url in "${OPTIONAL_NODE_REPOS_NO_PIP[@]}"; do
    _clone_or_update_node_repo "$repo_url" "false"
  done

  log_info "$(t nodes_more_via_manager)"
}
