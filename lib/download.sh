#!/usr/bin/env bash
# lib/download.sh — téléchargement générique depuis Hugging Face, avec reprise,
# vérification de taille, et somme SHA256 optionnelle.
#
# Utilise exclusivement `hf download` / `huggingface-cli download` : ces
# outils parlent nativement le protocole Xet (API de reconstruction CAS +
# URLs présignées par bloc de ~64 Mo, renouvelées au fil du téléchargement)
# plutôt qu'une URL unique obtenue une fois puis réutilisée jusqu'au bout.
# C'est ce qui les rend fiables sur les très gros fichiers (dizaines de Go) :
# aria2c a été retiré de ce chemin car il ne fait que suivre la redirection
# HTTP de `resolve/main/...`, ce qui bascule sur le pont LFS legacy avec une
# URL présignée à durée de vie courte (~1h) — suffisant pour la majorité du
# transfert, mais pouvant expirer en toute fin de téléchargement sur un gros
# fichier, d'où des 403 aléatoires vers 90-95%. Voir CHANGELOG.md.
#
# La licence du dépôt doit déjà avoir été acceptée par l'utilisateur sur
# huggingface.co — voir lib/huggingface.sh. Rien ici ne contourne un accès
# gated : un token sans accès obtient un 401/403, point.

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
  # download_hf_file <repo> <path_dans_repo> <dossier_racine_destination>
  # <dossier_racine_destination> doit être la racine sous laquelle `hf
  # download`/`huggingface-cli download --local-dir` reconstruit lui-même le
  # sous-chemin du dépôt (ex. models/, pas models/diffusion_models/) : ces
  # outils répliquent le chemin relatif complet du dépôt sous --local-dir
  # (documenté : "the file structure from the repo will be replicated in
  # this location"). Contrat différent de l'ancien téléchargement aria2c, qui
  # écrivait vers un fichier fixe dest_dir/filename sans reconstruire aucune
  # arborescence lui-même — d'où un double sous-dossier si l'appelant fournit
  # déjà le dossier final. Voir CHANGELOG.md.
  local repo="$1" path="$2" dest_dir="$3"
  local filename; filename="$(basename "$path")"
  local dest_file="${dest_dir}/${path}"

  mkdir -p "$dest_dir"

  if [[ -f "$dest_file" ]]; then
    local expected; expected="$(remote_content_length "$repo" "$path")"
    if verify_local_file "$dest_file" "$expected" "$filename"; then
      log_ok "${filename} déjà présent et valide, téléchargement sauté."
      return 0
    fi
    log_warn "${filename} présent mais invalide/incomplet, nouveau téléchargement."
  fi

  announce_download "$filename"

  local attempt=1
  while (( attempt <= DOWNLOAD_MAX_RETRIES )); do
    [[ "$attempt" -gt 1 ]] && log_info "Nouvelle tentative pour ${filename} (${attempt}/${DOWNLOAD_MAX_RETRIES})..."

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
  local filename; filename="$(basename "$path")"
  local dest_file="${dest_dir}/${path}"
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  export HF_XET_HIGH_PERFORMANCE=1     # accélérateur Xet actuel
  # `hf`/`huggingface-cli download` affichent nativement une barre par
  # fichier (tqdm/rich) avec nom, %, taille téléchargée/totale, débit et ETA
  # — on ne fait que la laisser passer jusqu'au terminal via
  # run_with_progress() au lieu de l'avaler dans le log (comportement
  # précédent), sans réimplémenter de parseur de progression.
  export HF_HUB_DISABLE_PROGRESS_BARS=0
  detect_hf_cli
  local ok=0
  if [[ "$HF_CLI" == "hf" ]]; then
    run_with_progress -- hf download "$repo" "$path" --local-dir "$dest_dir" || { deactivate; return 1; }

    if [[ ! -f "$dest_file" ]]; then
        log_error "Le fichier attendu est introuvable : $dest_file"
        deactivate
        return 1
    fi

    ok=1

  elif [[ "$HF_CLI" == "huggingface-cli" ]]; then
    run_with_progress -- huggingface-cli download "$repo" "$path" --local-dir "$dest_dir" || { deactivate; return 1; }

    if [[ ! -f "$dest_file" ]]; then
        log_error "Le fichier attendu est introuvable : $dest_file"
        deactivate
        return 1
    fi

    ok=1
  fi
  deactivate
  [[ "$ok" == "1" ]]
}
