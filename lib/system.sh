#!/usr/bin/env bash
# lib/system.sh — dépendances système (paquets apt).

install_system_packages() {
  log_step "Installation des paquets système"

  if ! require_cmd apt-get; then
    log_warn "apt-get introuvable (image non-Debian ?). On suppose que git/wget/curl/ffmpeg/aria2/python3 sont déjà présents."
    return 0
  fi

  local pkgs=(git wget curl aria2 ffmpeg unzip python3 python3-venv python3-pip build-essential ca-certificates)
  local missing=()
  for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    log_ok "Tous les paquets système requis sont déjà installés."
  else
    log_info "Paquets manquants : ${missing[*]}"
    local sudo_cmd=""
    if [[ "$(id -u)" -ne 0 ]]; then
      require_cmd sudo && sudo_cmd="sudo"
    fi
    # `apt-get update` peut renvoyer une erreur à cause d'un dépôt tiers cassé
    # (ex: un dépôt ajouté manuellement sur l'image de base) sans que cela
    # empêche d'installer nos paquets depuis les dépôts Ubuntu officiels. On
    # ne bloque donc pas dessus, seule l'installation elle-même est requise.
    if ! $sudo_cmd apt-get update -y >>"$LOG_FILE" 2>&1; then
      log_warn "apt-get update a rencontré une erreur (dépôt tiers indisponible ?) — on tente l'installation quand même."
    fi
    if ! retry "$DOWNLOAD_MAX_RETRIES" $sudo_cmd apt-get install -y --no-install-recommends "${missing[@]}" >>"$LOG_FILE" 2>&1; then
      log_error "Échec d'installation de : ${missing[*]}"
      log_error "Consultez ${LOG_FILE} — un dépôt apt tiers cassé peut être en cause (voir 'apt-get update' ci-dessus)."
      exit 1
    fi
    log_ok "Paquets système installés : ${missing[*]}"
  fi

  # pip à jour (pour l'utilisateur système ; le venv aura sa propre mise à jour)
  if require_cmd python3; then
    python3 -m pip install --upgrade pip --quiet >>"$LOG_FILE" 2>&1 || \
      log_warn "Impossible de mettre à jour pip au niveau système (pas bloquant, le venv gère sa propre version)."
  fi

  log_ok "Dépendances système prêtes."
}
