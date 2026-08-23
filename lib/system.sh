#!/usr/bin/env bash
# lib/system.sh — dépendances système (paquets apt).

install_system_packages() {
  log_step "$(t sys_step)"

  if ! require_cmd apt-get; then
    log_warn "$(t sys_no_apt)"
    return 0
  fi

  # python3-dev : fournit Python.h, requis pour compiler TOUTE extension
  # C/C++/CUDA qui inclut <Python.h> (SageAttention et d'autres nœuds
  # custom en dépendent). Sans lui, build-essential seul (g++/make) ne
  # suffit pas : la compilation échoue avec "fatal error: Python.h: No
  # such file or directory" (cf. post-mortem SageAttention).
  # ninja-build : accélère fortement la compilation d'extensions PyTorch
  # (backend "ninja" au lieu du repli "slow distutils backend" utilisé
  # sinon par torch.utils.cpp_extension) — non bloquant s'il manque, mais
  # rend la compilation SageAttention nettement plus lente.
  local pkgs=(git wget curl aria2 ffmpeg unzip tmux python3 python3-venv python3-pip python3-dev ninja-build build-essential ca-certificates)
  local missing=()
  for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    log_ok "$(t sys_all_present)"
  else
    log_info "$(t sys_missing "${missing[*]}")"
    local sudo_cmd=""
    if [[ "$(id -u)" -ne 0 ]]; then
      require_cmd sudo && sudo_cmd="sudo"
    fi
    # `apt-get update` peut renvoyer une erreur à cause d'un dépôt tiers cassé
    # (ex: un dépôt ajouté manuellement sur l'image de base) sans que cela
    # empêche d'installer nos paquets depuis les dépôts Ubuntu officiels. On
    # ne bloque donc pas dessus, seule l'installation elle-même est requise.
    if ! $sudo_cmd apt-get update -y >>"$LOG_FILE" 2>&1; then
      log_warn "$(t sys_apt_update_warn)"
    fi
    if ! retry "$DOWNLOAD_MAX_RETRIES" $sudo_cmd apt-get install -y --no-install-recommends "${missing[@]}" >>"$LOG_FILE" 2>&1; then
      log_error "$(t sys_install_failed "${missing[*]}")"
      log_error "$(t sys_install_failed_hint "$LOG_FILE")"
      exit 1
    fi
    log_ok "$(t sys_installed "${missing[*]}")"
  fi

  # pip à jour (pour l'utilisateur système ; le venv aura sa propre mise à jour)
  if require_cmd python3; then
    python3 -m pip install --upgrade pip --quiet >>"$LOG_FILE" 2>&1 || \
      log_warn "$(t sys_pip_update_failed)"
  fi

  log_ok "$(t sys_ready)"
}
