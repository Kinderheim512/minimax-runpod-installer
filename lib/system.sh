#!/usr/bin/env bash
# lib/system.sh — dépendances système (paquets apt).

install_system_packages() {
  log_step "Installation des paquets système"

  if ! require_cmd apt-get; then
    log_warn "apt-get introuvable (image non-Debian ?). On suppose que git/wget/curl/ffmpeg/aria2/python3 sont déjà présents."
    return 0
  fi

  local pkgs=(git wget curl aria2 ffmpeg unzip tmux python3 python3-venv python3-pip build-essential ca-certificates)
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

################################################################################
# ensure_nvidia_cuda_apt_repo() — installe le dépôt apt officiel NVIDIA
# (paquet cuda-keyring) si absent, seul moyen d'obtenir les paquets
# `cuda-toolkit-*` via apt : ils ne font PAS partie des dépôts Ubuntu
# standards, contrairement aux paquets installés par install_system_packages()
# ci-dessus. Sans ce dépôt, `apt-get install cuda-toolkit-X-Y` échoue
# systématiquement, quelle que soit la version demandée — pas une question de
# version indisponible, le paquet lui-même est introuvable pour apt.
#
# Repéré en pratique : l'image Docker de ce projet part de `ubuntu:22.04` nu
# (délibéré, voir Dockerfile — les wheels PyTorch embarquent déjà leur propre
# runtime CUDA, un toolkit système complet n'était censé être nécessaire que
# pour la compilation optionnelle de SageAttention) et n'a jamais ce dépôt —
# ce qui faisait échouer À CHAQUE FOIS l'installation du toolkit tentée par
# install_sageattention()/bake_sageattention_best_guess() (lib/python.sh),
# aussi bien à la construction de l'image qu'au démarrage du conteneur.
#
# Idempotent et best-effort : ne fait rien si le dépôt est déjà configuré
# (cas d'une image de base NVIDIA/CUDA officielle, qui l'a déjà) ; en cas
# d'échec (réseau, architecture non gérée...), avertit et retourne 1 sans
# jamais faire sortir install.sh/docker-build-steps.sh — les appelants
# (install_sageattention, bake_sageattention_best_guess) savent déjà sauter
# SageAttention proprement si le toolkit reste indisponible après coup.
################################################################################
ensure_nvidia_cuda_apt_repo() {
  require_cmd apt-get || return 1

  # Déjà configuré (image de base NVIDIA/CUDA officielle, ou appel précédent
  # dans cette même session) : rien à faire. dpkg -s est la vérification
  # fiable (le nom du fichier .list posé par le paquet peut varier), pas une
  # simple présence de fichier dans sources.list.d/.
  if dpkg -s cuda-keyring >/dev/null 2>&1; then
    return 0
  fi

  local sudo_cmd=""
  [[ "$(id -u)" -ne 0 ]] && require_cmd sudo && sudo_cmd="sudo"

  # Détection distro/arch -> nom de chemin attendu par le dépôt NVIDIA
  # (developer.download.nvidia.com/compute/cuda/repos/<distro>/<arch>/).
  # Seules les combinaisons couvertes par les images RunPod usuelles sont
  # gérées ; toute autre combinaison retourne 1 proprement plutôt que de
  # deviner une URL invalide.
  local distro="" arch=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    case "${ID:-}/${VERSION_ID:-}" in
      ubuntu/22.04) distro="ubuntu2204" ;;
      ubuntu/24.04) distro="ubuntu2404" ;;
      ubuntu/20.04) distro="ubuntu2004" ;;
      *) ;;
    esac
  fi
  case "$(uname -m)" in
    x86_64) arch="x86_64" ;;
    aarch64|arm64) arch="sbsa" ;;
    *) ;;
  esac

  if [[ -z "$distro" || -z "$arch" ]]; then
    log_warn "Distribution/architecture non reconnue pour le dépôt apt NVIDIA CUDA ($(uname -m), $(source /etc/os-release 2>/dev/null; echo "${ID:-inconnu} ${VERSION_ID:-}")) — SageAttention restera indisponible via cuda-toolkit apt sur cette image."
    return 1
  fi

  local keyring_url="https://developer.download.nvidia.com/compute/cuda/repos/${distro}/${arch}/cuda-keyring_1.1-1_all.deb"
  log_info "Dépôt apt NVIDIA CUDA absent — installation de cuda-keyring (${distro}/${arch}) pour rendre les paquets cuda-toolkit-* disponibles..."

  local tmp_deb
  tmp_deb="$(mktemp --suffix=.deb)"
  if ! retry "$DOWNLOAD_MAX_RETRIES" curl -fsSL "$keyring_url" -o "$tmp_deb" >>"$LOG_FILE" 2>&1; then
    log_warn "Échec du téléchargement de cuda-keyring (${keyring_url}) — SageAttention restera indisponible via cuda-toolkit apt sur cette image (consultez ${LOG_FILE})."
    rm -f "$tmp_deb"
    return 1
  fi

  if ! $sudo_cmd dpkg -i "$tmp_deb" >>"$LOG_FILE" 2>&1; then
    log_warn "Échec d'installation de cuda-keyring — SageAttention restera indisponible via cuda-toolkit apt sur cette image (consultez ${LOG_FILE})."
    rm -f "$tmp_deb"
    return 1
  fi
  rm -f "$tmp_deb"

  if ! $sudo_cmd apt-get update -y >>"$LOG_FILE" 2>&1; then
    log_warn "apt-get update a échoué après l'ajout du dépôt NVIDIA CUDA — on tente quand même l'installation du toolkit."
  fi

  log_ok "Dépôt apt NVIDIA CUDA installé (cuda-keyring) — cuda-toolkit-* est maintenant disponible via apt."
}
