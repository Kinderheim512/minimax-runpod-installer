#!/usr/bin/env bash
# lib/download.sh — téléchargement générique depuis Hugging Face, avec reprise,
# vérification de taille, et somme SHA256 optionnelle.
#
# Deux chemins possibles :
#   1) `hf download` / `huggingface-cli download` (par défaut) — gère
#      l'authentification, le cache, la reprise et l'intégrité nativement.
#   2) aria2c en téléchargement multi-connexions sur l'URL resolve/ signée par
#      le token de l'utilisateur (USE_ARIA2=true) — plus rapide sur gros
#      fichiers, reprise via `-c`. Retombe sur (1) en cas d'échec.
#
# Dans les deux cas, la licence du dépôt doit déjà avoir été acceptée par
# l'utilisateur sur huggingface.co — voir lib/huggingface.sh. Rien ici ne
# contourne un accès gated : un token sans accès obtient un 401/403, point.

remote_content_length() {
  # remote_content_length <repo> <path_dans_repo>
  local repo="$1" path="$2"
  local url="https://huggingface.co/${repo}/resolve/main/${path}"
  local headers=(-sIL)
  [[ -n "$HF_TOKEN" ]] && headers+=(-H "Authorization: Bearer ${HF_TOKEN}")
  curl "${headers[@]}" "$url" 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="content-length"{v=$2} END{print v}'
}

verify_local_file() {
  # verify_local_file <fichier_local> <taille_distante_attendue_ou_vide> <nom_pour_sha256_lookup>
  local file="$1" expected_size="$2" name="$3"

  [[ -f "$file" ]] || { log_warn "Fichier absent après téléchargement : ${file}"; return 1; }

  local local_size
  local_size="$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null)"

  if [[ -n "$expected_size" && "$expected_size" =~ ^[0-9]+$ ]]; then
    if [[ "$local_size" != "$expected_size" ]]; then
      log_warn "Taille inattendue pour $(basename "$file") : ${local_size} octets (attendu ${expected_size})."
      return 1
    fi
  else
    log_warn "Taille distante inconnue pour $(basename "$file"), vérification de taille sautée."
  fi

  if [[ -n "${MODEL_SHA256[$name]:-}" ]]; then
    log_info "Vérification SHA256 de ${name}..."
    local got; got="$(sha256sum "$file" | awk '{print $1}')"
    if [[ "$got" != "${MODEL_SHA256[$name]}" ]]; then
      log_error "SHA256 invalide pour ${name} (attendu ${MODEL_SHA256[$name]}, obtenu ${got})."
      return 1
    fi
    log_ok "SHA256 vérifié pour ${name}."
  fi

  return 0
}

download_hf_file() {
  # download_hf_file <repo> <path_dans_repo> <dossier_destination>
  local repo="$1" path="$2" dest_dir="$3"
  local filename; filename="$(basename "$path")"
  local dest_file="${dest_dir}/${filename}"

  mkdir -p "$dest_dir"

  if [[ -f "$dest_file" ]]; then
    local expected; expected="$(remote_content_length "$repo" "$path")"
    if verify_local_file "$dest_file" "$expected" "$filename"; then
      log_ok "${filename} déjà présent et valide, téléchargement sauté."
      return 0
    fi
    log_warn "${filename} présent mais invalide/incomplet, nouveau téléchargement."
  fi

  local attempt=1
  while (( attempt <= DOWNLOAD_MAX_RETRIES )); do
    log_info "Téléchargement de ${filename} (tentative ${attempt}/${DOWNLOAD_MAX_RETRIES})..."

    if [[ "$USE_ARIA2" == "true" ]] && require_cmd aria2c; then
      _download_via_aria2 "$repo" "$path" "$dest_dir" && break
      log_warn "aria2c a échoué, repli sur ${HF_CLI:-hf}."
    fi

    _download_via_hf_cli "$repo" "$path" "$dest_dir" && break

    ((attempt++))
    sleep 5
  done

  local expected; expected="$(remote_content_length "$repo" "$path")"
  if ! verify_local_file "$dest_file" "$expected" "$filename"; then
    log_error "Échec de vérification pour ${filename} après ${DOWNLOAD_MAX_RETRIES} tentatives."
    return 1
  fi

  log_ok "${filename} téléchargé et vérifié ($(human_gb "${expected:-0}"))."
}

_download_via_hf_cli() {
  local repo="$1" path="$2" dest_dir="$3"
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  export HF_HUB_ENABLE_HF_TRANSFER=1   # legacy accelerator, ignoré (avec avertissement) par les versions récentes de huggingface_hub
  export HF_XET_HIGH_PERFORMANCE=1     # accélérateur Xet actuel
  detect_hf_cli
  local ok=0
  if [[ "$HF_CLI" == "hf" ]]; then
    hf download "$repo" "$path" --local-dir "$dest_dir" >>"$LOG_FILE" 2>&1 || return 1

    if [[ ! -f "$dest_file" ]]; then
        log_error "Le fichier attendu est introuvable : $dest_file"
        return 1
    fi

    ok=1

elif [[ "$HF_CLI" == "huggingface-cli" ]]; then
    huggingface-cli download "$repo" "$path" --local-dir "$dest_dir" >>"$LOG_FILE" 2>&1 || return 1

    if [[ ! -f "$dest_file" ]]; then
        log_error "Le fichier attendu est introuvable : $dest_file"
        return 1
    fi

    ok=1
fi
    deactivate
  [[ "$ok" == "1" ]]
}

_download_via_aria2() {
  local repo="$1" path="$2" dest_dir="$3"
  local filename; filename="$(basename "$path")"
  local url="https://huggingface.co/${repo}/resolve/main/${path}"
  local sub_dir; sub_dir="$(dirname "$path")"
  local out_dir="$dest_dir"
  [[ "$sub_dir" != "." ]] && out_dir="${dest_dir}"
  mkdir -p "$out_dir"

  local token="${HF_TOKEN}"
  if [[ -z "$token" ]]; then
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    token="$(python -c 'from huggingface_hub import HfFolder; print(HfFolder.get_token() or "")' 2>/dev/null)"
    deactivate
  fi

  local header_arg=()
  [[ -n "$token" ]] && header_arg=(--header="Authorization: Bearer ${token}")

  aria2c -x "$ARIA2_CONNECTIONS" -s "$ARIA2_CONNECTIONS" -c \
    "${header_arg[@]}" \
    -d "$out_dir" -o "$filename" \
    --summary-interval=30 --console-log-level=warn \
    "$url" >>"$LOG_FILE" 2>&1
}
