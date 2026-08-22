#!/usr/bin/env bash
# lib/comfyui.sh — installation et mise à jour du dépôt ComfyUI.
#
# Le support natif de MiniMax H3 (nœuds MiniMaxH3ImageToVideo /
# MiniMaxH3ReferenceToVideo) est arrivé dans ComfyUI >= 0.30.0. Par défaut
# (COMFYUI_RELEASE_MODE=release), on installe la dernière RELEASE stable
# taguée (vX.Y.Z) plutôt que le dernier commit de la branche : reproductible
# d'un jour à l'autre et jamais un commit master non testé. L'ancien
# comportement (suivre ${COMFYUI_BRANCH} au jour le jour) reste disponible
# via COMFYUI_RELEASE_MODE=branch. Voir resolve_comfyui_target() ci-dessous
# pour le seul endroit qui décide de la cible.

# resolve_comfyui_release_tag
# Dernier tag de release stable ("vX.Y.Z" exactement, pré-releases/rc
# exclues) du dépôt COMFYUI_REPO, via `git ls-remote --tags` — pas d'appel à
# l'API GitHub, donc pas de rate-limit ni de dépendance à jq/curl pour ça.
# Retourne un code non nul (rien sur stdout) si aucun tag exploitable n'est
# trouvé.
resolve_comfyui_release_tag() {
  local best
  best="$(git ls-remote --tags --refs "$COMFYUI_REPO" 2>/dev/null \
    | awk '{print $2}' | sed 's#^refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -V | tail -n1)"
  [[ -n "$best" ]] || return 1
  printf '%s' "$best"
}

# resolve_comfyui_target
# Seul endroit du projet qui décide de la cible git à cloner/checkout pour
# ComfyUI (hors épinglage explicite COMFYUI_COMMIT, géré séparément par
# pin_comfyui_commit — prioritaire, appliqué après). Retourne un tag
# ("vX.Y.Z") en mode "release", ou ${COMFYUI_BRANCH} en mode "branch" ou en
# repli si aucun tag n'a pu être résolu.
resolve_comfyui_target() {
  if [[ "$COMFYUI_RELEASE_MODE" != "release" ]]; then
    printf '%s' "$COMFYUI_BRANCH"
    return 0
  fi

  local tag
  if tag="$(resolve_comfyui_release_tag)" && [[ -n "$tag" ]]; then
    printf '%s' "$tag"
    return 0
  fi

  log_warn "$(t comfyui_release_resolve_failed "$COMFYUI_BRANCH")"
  printf '%s' "$COMFYUI_BRANCH"
}

clone_or_update_comfyui() {
  log_step "$(t comfyui_install_step)"

  mkdir -p "$(dirname "$INSTALL_DIR")"

  local target
  target="$(resolve_comfyui_target)"
  if [[ "$COMFYUI_RELEASE_MODE" == "release" ]]; then
    log_info "$(t comfyui_latest_release_detected "$target")"
  fi

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log_info "$(t comfyui_already_cloned "$INSTALL_DIR")"
    update_comfyui "$target"
  elif [[ -e "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    log_error "$(t comfyui_dir_conflict "$INSTALL_DIR")"
    log_error "$(t comfyui_dir_conflict_fix)"
    exit 1
  else
    log_info "$(t comfyui_cloning "$COMFYUI_REPO" "$target" "$INSTALL_DIR")"
    retry "$DOWNLOAD_MAX_RETRIES" git clone --branch "$target" "$COMFYUI_REPO" "$INSTALL_DIR" >>"$LOG_FILE" 2>&1
    log_ok "$(t comfyui_cloned)"
  fi

  pin_comfyui_commit

  print_comfyui_version
}

# Épinglage optionnel sur un commit précis (COMFYUI_COMMIT dans config.env).
# No-op si la variable est vide : comportement par défaut inchangé (on reste
# sur la cible résolue par resolve_comfyui_target() — tag de release ou
# branche selon COMFYUI_RELEASE_MODE, cf. clone_or_update_comfyui /
# update_comfyui ci-dessus). Appelé juste après le clone ou la mise à jour,
# donc après que cette cible a déjà été checkout.
pin_comfyui_commit() {
  [[ -n "${COMFYUI_COMMIT:-}" ]] || return 0

  log_step "$(t comfyui_pin_step "$COMFYUI_COMMIT")"

  local dirty
  dirty="$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null || true)"
  if [[ -n "$dirty" ]]; then
    log_warn "$(t comfyui_pin_local_changes "$INSTALL_DIR")"
    log_warn "$(t comfyui_pin_local_changes_fix)"
    return 0
  fi

  # Le commit demandé peut ne pas encore être présent localement (clone peu
  # profond, ou commit plus récent que le dernier fetch) : on le récupère
  # explicitement avant le checkout plutôt que de supposer qu'il y est déjà.
  if ! retry "$DOWNLOAD_MAX_RETRIES" git -C "$INSTALL_DIR" fetch origin "$COMFYUI_COMMIT" >>"$LOG_FILE" 2>&1; then
    log_warn "$(t comfyui_pin_fetch_failed "$COMFYUI_COMMIT")"
  fi

  if ! git -C "$INSTALL_DIR" checkout "$COMFYUI_COMMIT" >>"$LOG_FILE" 2>&1; then
    log_error "$(t comfyui_pin_checkout_failed "$COMFYUI_COMMIT")"
    log_error "$(t comfyui_pin_checkout_failed_fix "$COMFYUI_REPO")"
    exit 1
  fi

  log_ok "$(t comfyui_pinned "$COMFYUI_COMMIT")"
}

update_comfyui() {
  # $1 : cible résolue par resolve_comfyui_target() (tag "vX.Y.Z" en mode
  # "release", nom de branche en mode "branch"). Argument requis : cette
  # fonction ne recalcule jamais la cible elle-même, pour garder un seul
  # endroit (resolve_comfyui_target) qui décide.
  local target="$1"
  local dirty
  dirty="$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null || true)"
  if [[ -n "$dirty" ]]; then
    log_warn "$(t comfyui_update_local_changes "$INSTALL_DIR")"
    log_warn "$(t comfyui_update_local_changes_fix)"
    return 0
  fi
  {
    retry "$DOWNLOAD_MAX_RETRIES" git -C "$INSTALL_DIR" fetch --tags origin
    git -C "$INSTALL_DIR" checkout "$target"
    # En mode "release", $target est un tag figé : le checkout suffit, pas
    # de pull. En mode "branch" (ancien comportement), on avance jusqu'au
    # dernier commit distant de la branche.
    if [[ "$COMFYUI_RELEASE_MODE" != "release" ]]; then
      retry "$DOWNLOAD_MAX_RETRIES" git -C "$INSTALL_DIR" pull --ff-only origin "$target"
    fi
  } >>"$LOG_FILE" 2>&1
  log_ok "$(t comfyui_updated "$target")"
}

print_comfyui_version() {
  local rev version_str
  rev="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || t comfyui_unknown)"
  version_str="$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || t comfyui_unknown)"
  log_info "$(t comfyui_version_line "$rev" "$version_str")"

  # Comparaison best-effort avec MIN_COMFYUI_VERSION (avertissement seulement,
  # ComfyUI ne tague pas systématiquement chaque commit de master).
  if [[ "$version_str" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
    local maj="${BASH_REMATCH[1]}" min="${BASH_REMATCH[2]}"
    IFS='.' read -r req_maj req_min _ <<< "$MIN_COMFYUI_VERSION"
    if (( maj < req_maj || (maj == req_maj && min < req_min) )); then
      log_warn "$(t comfyui_version_older_warn "$version_str" "$MIN_COMFYUI_VERSION")"
    else
      log_ok "$(t comfyui_version_ok "$MIN_COMFYUI_VERSION")"
    fi
  elif [[ "$COMFYUI_RELEASE_MODE" == "release" ]]; then
    log_info "$(t comfyui_version_unknown_release "$MIN_COMFYUI_VERSION")"
  else
    log_info "$(t comfyui_version_unknown_branch "$MIN_COMFYUI_VERSION" "$COMFYUI_BRANCH")"
  fi
}
