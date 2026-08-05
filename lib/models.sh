#!/usr/bin/env bash
# lib/models.sh — arborescence de modèles ComfyUI + téléchargement des poids
# MiniMax H3 officiels (dépôt Comfy-Org/MiniMax-H3, repackagé pour ComfyUI).
#
# Sources (vérifiées) :
#   https://huggingface.co/Comfy-Org/MiniMax-H3
#   https://docs.comfy.org/tutorials/video/minimax/minimax-h3
#   https://blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui
#
# Tailles indiquées à titre informatif pour l'estimation d'espace disque
# (relevées sur le dépôt HF au moment de l'écriture — vérifiées dynamiquement
# au téléchargement via la taille distante réelle, voir lib/download.sh).

create_model_folders() {
  log_step "Création de l'arborescence de modèles"
  local base="${INSTALL_DIR}/models"
  for d in checkpoints diffusion_models text_encoders vae clip controlnet loras upscale_models; do
    mkdir -p "${base}/${d}"
  done
  log_ok "Dossiers models/{checkpoints,diffusion_models,text_encoders,vae,clip,controlnet,loras,upscale_models} prêts."
}

# --- Manifeste des fichiers H3 -----------------------------------------------
# format : "sous_chemin_dans_le_repo|palier|go_approx"
H3_DIFFUSION_FL2VA=(
  "diffusion_models/minimax_h3_fl2va_bf16.safetensors|max|66.3"
  "diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors|balanced|34"
  "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors|light|21"
)
H3_DIFFUSION_REF2VA=(
  "diffusion_models/minimax_h3_ref2va_bf16.safetensors|max|66.3"
  "diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors|balanced|34"
  "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors|light|21"
)
H3_TEXT_ENCODER=(
  "text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors|max|51.5"
  "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors|balanced|27.1"
  "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|light|15.7"
)
H3_VAE=(
  "vae/minimax_h3_video_vae_fp16.safetensors"
  "vae/minimax_h3_audio_vae_fp32.safetensors"
)

_pick_for_tier() {
  # _pick_for_tier <tier> <array_name> -> écho le sous-chemin correspondant
  local tier="$1"; shift
  local -n arr="$1"
  for entry in "${arr[@]}"; do
    IFS='|' read -r subpath t size <<< "$entry"
    if [[ "$t" == "$tier" ]]; then echo "$subpath|$size"; return 0; fi
  done
  return 1
}

resolve_h3_tier() {
  local tier="$H3_TIER"
  if [[ "$tier" == "auto" ]]; then
    tier="${GPU_TIER_RECOMMENDED:-balanced}"
    log_info "H3_TIER=auto → palier '${tier}' choisi selon la VRAM détectée."
  fi
  echo "$tier"
}

estimate_download_size_gb() {
  local tier="$1"
  local workflows="$2"
  local total=0

  IFS=',' read -ra wf <<< "${workflows/all/t2v,i2v,r2v}"
  local need_fl2va="false" need_ref2va="false"
  for w in "${wf[@]}"; do
    case "$w" in t2v|i2v) need_fl2va="true";; r2v) need_ref2va="true";; esac
  done

  local entry size
  if [[ "$need_fl2va" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"; size="${entry##*|}"
    total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
  fi
  if [[ "$need_ref2va" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"; size="${entry##*|}"
    total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
  fi
  entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"; size="${entry##*|}"
  total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
  total=$(awk -v a="$total" 'BEGIN{print a+3}')  # marge ~3 Go pour les deux VAE

  echo "$total"
}

# Taille approximative (Go) des deux VAE — non tarifées par palier dans le
# manifeste (H3_VAE ne liste que les chemins). Somme = 3 Go, la même marge
# forfaitaire qu'utilisait déjà estimate_download_size_gb() ci-dessus, donc
# aucun changement de comportement pour les estimations "tout manque".
H3_VAE_SIZE_GB_VIDEO="2.7"
H3_VAE_SIZE_GB_AUDIO="0.3"

# get_model_size_gb <key> <tier> -> taille approximative (Go) sur stdout.
# Réutilise le même manifeste de tailles que estimate_download_size_gb() —
# aucune duplication des chiffres eux-mêmes, juste un accès par clé de modèle
# plutôt que par liste de workflows.
get_model_size_gb() {
  local key="$1"
  local tier="$2"
  local entry

  case "$key" in
    fl2va)
      entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"; echo "${entry##*|}"
      ;;
    ref2va)
      entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"; echo "${entry##*|}"
      ;;
    text_encoder)
      entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"; echo "${entry##*|}"
      ;;
    video_vae)
      echo "$H3_VAE_SIZE_GB_VIDEO"
      ;;
    audio_vae)
      echo "$H3_VAE_SIZE_GB_AUDIO"
      ;;
  esac
}

# estimate_missing_download_size_gb <tier>
# Contrairement à estimate_download_size_gb() (qui chiffre tout le
# workflow), ne totalise que les modèles marqués manquants dans
# H3_MODEL_MISSING — reflète donc le volume réellement téléchargé.
# Doit être appelée après collect_missing_models().
estimate_missing_download_size_gb() {
  local tier="$1"
  local total=0
  local key size

  for key in "${H3_MODEL_KEYS[@]}"; do
    if [[ "${H3_MODEL_MISSING[$key]:-true}" == "true" ]]; then
      size="$(get_model_size_gb "$key" "$tier")"
      total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
    fi
  done

  echo "$total"
}

# --- Téléchargement CivitAI --------------------------------------------------
# download_civitai_model <url> <dest_path>
# - crée le dossier de destination si nécessaire
# - reprend un téléchargement interrompu (-C -)
# - suit les redirections (-L)
# - échoue proprement sur erreur HTTP (--fail) et réessaie
download_civitai_model() {
  local url="$1"
  local dest="$2"
  local dest_dir; dest_dir="$(dirname "$dest")"
  mkdir -p "$dest_dir"

  local max_retries="${DOWNLOAD_MAX_RETRIES:-5}"
  local attempt=1

  log_info "Téléchargement (CivitAI) : $(basename "$dest")"
  while (( attempt <= max_retries )); do
    if curl -L -C - --fail --retry 3 --retry-delay 2 -o "$dest" "$url"; then
      log_ok "Téléchargé (CivitAI) : $(basename "$dest")"
      return 0
    fi
    log_warn "Échec du téléchargement CivitAI (tentative ${attempt}/${max_retries}) : $(basename "$dest")"
    attempt=$((attempt + 1))
    [[ $attempt -le $max_retries ]] && sleep 2
  done

  log_error "Échec définitif du téléchargement CivitAI : ${url}"
  return 1
}

# --- Dispatcher source pour les modèles de diffusion -------------------------
# download_diffusion_model <hf_subpath> <base_dir> <civitai_url> <source>
# Route vers HuggingFace (comportement historique, inchangé) ou CivitAI selon
# MODEL_SOURCE. Le nom de fichier final est toujours identique quel que soit
# la source (dérivé du sous-chemin HuggingFace), donc aucun autre script
# (workflows, verify, etc.) n'a besoin de savoir d'où vient le fichier.
download_diffusion_model() {
  local hf_subpath="$1"
  local base="$2"
  local civitai_url="$3"
  local source="$4"

  if [[ "$source" == "civitai" ]]; then
    local filename; filename="$(basename "$hf_subpath")"
    download_civitai_model "$civitai_url" "${base}/diffusion_models/${filename}"
  else
    download_hf_file "$H3_HF_REPO" "$hf_subpath" "$base"
  fi
}

# =============================================================================
# Détection locale des modèles déjà installés (aucun accès réseau ici)
# =============================================================================
#
# Chaque modèle final (palier "light", seul palier utilisé par
# download_h3_models) est décrit une seule fois : chemin relatif sous
# models/, taille minimale plausible (détecte un téléchargement coupé) et
# libellé affiché. C'est la seule source de vérité utilisée par le scan, la
# suppression des fichiers corrompus et le calcul des téléchargements
# manquants — donc aucune duplication entre ces étapes.

H3_MODEL_KEYS=(fl2va ref2va text_encoder video_vae audio_vae)

declare -A H3_MODEL_FILES=(
  [fl2va]="diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
  [ref2va]="diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"
  [text_encoder]="text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
  [video_vae]="vae/minimax_h3_video_vae_fp16.safetensors"
  [audio_vae]="vae/minimax_h3_audio_vae_fp32.safetensors"
)

# Seuils volontairement conservateurs : ils ne servent qu'à repérer un
# téléchargement interrompu, pas à valider un checksum exact.
declare -A H3_MODEL_MIN_BYTES=(
  [fl2va]=$((15 * 1024 * 1024 * 1024))          # > 15 Go
  [ref2va]=$((15 * 1024 * 1024 * 1024))         # > 15 Go
  [text_encoder]=$((10 * 1024 * 1024 * 1024))   # > 10 Go
  [video_vae]=$((3 * 1024 * 1024 * 1024))       # > 3 Go
  [audio_vae]=$((300 * 1024 * 1024))            # > 300 Mo
)

declare -A H3_MODEL_LABELS=(
  [fl2va]="FL2VA"
  [ref2va]="REF2VA"
  [text_encoder]="Text Encoder"
  [video_vae]="Video VAE"
  [audio_vae]="Audio VAE"
)

# État calculé par collect_missing_models() : "true"/"false" par clé.
declare -A H3_MODEL_MISSING=()

# --- Vérification d'intégrité SHA256 (optionnelle) --------------------------
#
# Deux sources possibles pour le SHA256 attendu d'un modèle, dans cet ordre :
#   1) MODEL_SHA256[key] — surcharge manuelle définie dans config.env
#      (tableau déjà déclaré là-bas, vide par défaut). Aucun accès réseau :
#      utilisée telle quelle si renseignée.
#   2) Si H3_VERIFY_SHA256_ONLINE=true, tentative de récupération du SHA256
#      publié par HuggingFace (en-tête X-Linked-Etag des fichiers LFS).
#      Coûte une requête réseau par modèle : désactivé par défaut pour
#      préserver un second lancement instantané et sans réseau.
# Si aucun SHA256 n'est disponible (cas par défaut), la validation retombe
# sur le contrôle de taille minimale déjà en place — comportement identique
# à avant l'ajout de cette fonctionnalité.
H3_VERIFY_SHA256_ONLINE="${H3_VERIFY_SHA256_ONLINE:-false}"

# MODEL_SHA256 est normalement déclaré dans config.env ; on se prémunit ici
# au cas où ce fichier serait chargé seul (tests, usage isolé du module).
if ! declare -p MODEL_SHA256 &>/dev/null; then
  declare -A MODEL_SHA256=()
fi

# compute_sha256 <path> -> empreinte SHA256 locale sur stdout.
# Retourne 1 si aucun outil de hachage n'est disponible sur le système.
compute_sha256() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  else
    return 1
  fi
}

# fetch_hf_sha256 <repo> <subpath> -> SHA256 publié par HuggingFace sur
# stdout, lu depuis l'en-tête X-Linked-Etag renvoyé pour les fichiers LFS.
# Retourne 1 si l'en-tête est absent (fichier non-LFS, erreur réseau, etc.).
fetch_hf_sha256() {
  local repo="$1"
  local subpath="$2"
  local url="https://huggingface.co/${repo}/resolve/main/${subpath}"
  local header sha

  header="$(curl -sIL --max-time 10 "$url" 2>/dev/null | tr -d '\r' | grep -i '^x-linked-etag:' | tail -n1)"
  [[ -z "$header" ]] && return 1
  sha="$(echo "$header" | grep -oE '[a-f0-9]{64}')"
  [[ -z "$sha" ]] && return 1
  echo "$sha"
}

# get_expected_sha256 <key> <hf_subpath> -> SHA256 attendu sur stdout, ou
# rien (et code 1) si aucun n'est disponible. Voir priorité ci-dessus.
get_expected_sha256() {
  local key="$1"
  local hf_subpath="$2"

  if [[ -n "${MODEL_SHA256[$key]:-}" ]]; then
    echo "${MODEL_SHA256[$key]}"
    return 0
  fi

  if [[ "$H3_VERIFY_SHA256_ONLINE" == "true" ]]; then
    fetch_hf_sha256 "$H3_HF_REPO" "$hf_subpath"
    return $?
  fi

  return 1
}

# model_file_size <path> -> taille en octets sur stdout, 0 si absent/illisible.
model_file_size() {
  local path="$1"
  [[ -e "$path" ]] || { echo 0; return 1; }
  stat -c%s "$path" 2>/dev/null || stat -f%z "$path" 2>/dev/null || wc -c < "$path" 2>/dev/null || echo 0
}

# model_exists <path>
model_exists() {
  [[ -f "$1" ]]
}

# model_is_valid <path> <min_bytes> [expected_sha256]
# Vrai si le fichier existe, fait plus de 0 octet, et atteint la taille
# minimale plausible pour ce modèle (détecte un téléchargement interrompu).
# Si un SHA256 attendu est fourni, il est vérifié en plus (et prime sur le
# résultat du seul contrôle de taille) ; sinon la taille suffit, comme avant.
model_is_valid() {
  local path="$1"
  local min_bytes="$2"
  local expected_sha256="${3:-}"
  local size

  model_exists "$path" || return 1
  size="$(model_file_size "$path")"
  [[ "$size" -gt 0 ]] || return 1
  if [[ -n "$min_bytes" && "$size" -lt "$min_bytes" ]]; then
    return 1
  fi

  if [[ -n "$expected_sha256" ]]; then
    local actual_sha256
    if actual_sha256="$(compute_sha256 "$path")"; then
      [[ "$actual_sha256" == "$expected_sha256" ]] || return 1
    else
      log_warn "sha256sum/shasum introuvable — vérification SHA256 ignorée pour $(basename "$path")."
    fi
  fi

  return 0
}

# remove_corrupted_model <path> — supprime un fichier absent/vide/incomplet
# après l'avoir signalé.
remove_corrupted_model() {
  local path="$1"
  echo "Corrupted or incomplete model detected:"
  echo "$(basename "$path")"
  rm -f "$path"
}

# collect_missing_models <base_dir>
# Étapes 1 à 3 : scanne les 5 modèles H3 sur disque, sans toucher au réseau.
# Supprime automatiquement tout fichier absent/vide/incomplet et le marque
# comme manquant. Remplit l'état global H3_MODEL_MISSING et affiche le
# récapitulatif ✓/✗ attendu par l'utilisateur.
collect_missing_models() {
  local base="$1"
  local key path full_path min_bytes label expected_sha256

  echo "------------------------------------------------"
  echo "Checking installed MiniMax H3 models..."

  for key in "${H3_MODEL_KEYS[@]}"; do
    path="${H3_MODEL_FILES[$key]}"
    full_path="${base}/${path}"
    min_bytes="${H3_MODEL_MIN_BYTES[$key]}"
    label="${H3_MODEL_LABELS[$key]}"
    # $path est déjà le sous-chemin relatif au dépôt HF (identique à
    # H3_MODEL_FILES), donc réutilisable tel quel pour un éventuel fetch.
    expected_sha256="$(get_expected_sha256 "$key" "$path")"

    if model_is_valid "$full_path" "$min_bytes" "$expected_sha256"; then
      echo "✓ ${label}"
      H3_MODEL_MISSING[$key]="false"
    else
      # Fichier présent mais invalide (0 octet / incomplet) -> nettoyage.
      if [[ -e "$full_path" ]]; then
        remove_corrupted_model "$full_path"
      fi
      echo "✗ ${label}"
      H3_MODEL_MISSING[$key]="true"
    fi
  done
}

# any_model_missing — vrai si au moins un modèle doit être téléchargé.
any_model_missing() {
  local key
  for key in "${H3_MODEL_KEYS[@]}"; do
    [[ "${H3_MODEL_MISSING[$key]:-true}" == "true" ]] && return 0
  done
  return 1
}

# need_hf_download <model_source>
# Vrai si au moins un téléchargement HuggingFace est réellement nécessaire :
# Text Encoder / VAE manquants (toujours servis par HF), ou modèles de
# diffusion manquants alors que MODEL_SOURCE=huggingface.
need_hf_download() {
  local source="$1"

  [[ "${H3_MODEL_MISSING[text_encoder]}" == "true" ]] && return 0
  [[ "${H3_MODEL_MISSING[video_vae]}" == "true" ]] && return 0
  [[ "${H3_MODEL_MISSING[audio_vae]}" == "true" ]] && return 0

  if [[ "$source" != "civitai" ]]; then
    [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]] && return 0
    [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]] && return 0
  fi
  return 1
}

# need_civitai_download <model_source>
# Vrai si au moins un modèle de diffusion manquant doit venir de CivitAI.
need_civitai_download() {
  local source="$1"

  [[ "$source" == "civitai" ]] || return 1
  [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]] && return 0
  [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]] && return 0
  return 1
}

# download_missing_models <base_dir> <model_source> <tier>
# Ne télécharge que les modèles marqués manquants dans H3_MODEL_MISSING.
# N'appelle hf_check_h3_access() que si need_hf_download() est vrai, et ne
# lance jamais curl (CivitAI) si les modèles de diffusion sont déjà valides.
download_missing_models() {
  local base="$1"
  local model_source="$2"
  local tier="$3"

  if ! any_model_missing; then
    echo "------------------------------------------------"
    echo "All required MiniMax H3 models are already installed."
    echo "Skipping all downloads."
    echo "Skipping HuggingFace access check."
    echo "Continuing installation..."
    echo "------------------------------------------------"
    return 0
  fi

  if need_hf_download "$model_source"; then
    if ! hf_check_h3_access; then
      log_error "Téléchargement des modèles annulé : accès au dépôt non confirmé."
      log_error "Acceptez la licence puis relancez : bash install.sh --only-models"
      return 1
    fi
  fi

  local entry subpath

  if [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"; subpath="${entry%%|*}"
    download_diffusion_model "$subpath" "$base" "$H3_CIVITAI_FL2VA_URL" "$model_source"
  fi

  if [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"; subpath="${entry%%|*}"
    download_diffusion_model "$subpath" "$base" "$H3_CIVITAI_REF2VA_URL" "$model_source"
  fi

  # Text Encoder et VAE restent toujours servis par HuggingFace, quel que
  # soit MODEL_SOURCE (CivitAI ne les propose pas).
  if [[ "${H3_MODEL_MISSING[text_encoder]}" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"; subpath="${entry%%|*}"
    download_hf_file "$H3_HF_REPO" "$subpath" "$base"
  fi

  if [[ "${H3_MODEL_MISSING[video_vae]}" == "true" ]]; then
    download_hf_file "$H3_HF_REPO" "${H3_MODEL_FILES[video_vae]}" "$base"
  fi

  if [[ "${H3_MODEL_MISSING[audio_vae]}" == "true" ]]; then
    download_hf_file "$H3_HF_REPO" "${H3_MODEL_FILES[audio_vae]}" "$base"
  fi

  echo "------------------------------------------------"
  return 0
}

download_h3_models() {
  log_step "Téléchargement des modèles MiniMax H3"

  # Forcé sur le palier "light" (pruned_int8_convrot + nvfp4_awq). Workflows
  # t2v + i2v + r2v : les trois workflows officiels partagent le même
  # encodeur de texte et les mêmes VAE, mais t2v/i2v ont besoin du modèle
  # fl2va tandis que r2v a besoin du modèle ref2va — les deux UNet sont donc
  # nécessaires simultanément, d'où les trois workflows listés ici.
  local tier="light"
  local workflows="t2v,i2v,r2v"

  local model_source="${MODEL_SOURCE:-huggingface}"
  case "$model_source" in
    huggingface|civitai) ;;
    *)
      log_warn "MODEL_SOURCE='${model_source}' inconnu — retour à 'huggingface'."
      model_source="huggingface"
      ;;
  esac

  echo "------------------------------------------------"
  if [[ "$model_source" == "civitai" ]]; then
    echo "Selected Model Source : CivitAI"
  else
    echo "Selected Model Source : HuggingFace"
  fi

  local base="${INSTALL_DIR}/models"

  # --- Étapes 1-3 : scan disque local, réparation, aucun accès réseau ------
  collect_missing_models "$base"

  # --- Cas 1 : tout est déjà installé et valide -----------------------------
  if ! any_model_missing; then
    download_missing_models "$base" "$model_source" "$tier"
    log_ok "Modèles MiniMax H3 déjà installés (palier ${tier}, source ${model_source})."
    return 0
  fi

  # --- Cas 2 : au moins un modèle manque -> estimation espace disque avant
  #             de contacter le réseau, puis téléchargement ciblé -----------
  # Estimation basée uniquement sur les modèles marqués manquants (et non
  # sur l'ensemble du workflow) : si seul le Video VAE manque, on annonce
  # ~2.7 Go et non ~60 Go.
  local est_gb; est_gb="$(estimate_missing_download_size_gb "$tier")"
  local free_gb; free_gb="$(free_disk_gb "$INSTALL_DIR")"
  log_info "Palier retenu : ${tier} — workflows : ${workflows}"
  log_info "Espace requis (estimation, modèles manquants uniquement) : ~${est_gb} Go — espace libre : ${free_gb:-inconnu} Go"

  if [[ -n "$free_gb" ]] && awk -v f="$free_gb" -v e="$est_gb" 'BEGIN{exit !(f < e)}'; then
    log_warn "Espace disque possiblement insuffisant (${free_gb} Go libres pour ~${est_gb} Go requis)."
    confirm "Continuer quand même ?" || { log_error "Téléchargement annulé par l'utilisateur."; return 1; }
  fi

  download_missing_models "$base" "$model_source" "$tier" || return 1

  log_ok "Modèles MiniMax H3 (palier ${tier}, workflows ${workflows}, source ${model_source}) téléchargés."
}
