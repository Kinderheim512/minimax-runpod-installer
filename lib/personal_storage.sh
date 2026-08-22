#!/usr/bin/env bash
# lib/personal_storage.sh — sauvegarde/restauration des LoRAs, presets et
# outputs perso, indépendamment du Network Volume RunPod (payant, lié à un
# seul datacenter — voir la note d'objectif dans config.env). Sert aussi de
# mécanisme de "configuration perso" pour les LoRAs à télécharger, les nœuds
# custom et les workflows souhaités — voir la section MANIFESTES ci-dessous.
#
# Deux backends indépendants, qui peuvent cohabiter :
#   - Hugging Face (PERSONAL_STORAGE_HF_REPO) : coffre privé complet
#     (loras/presets/outputs/workflows + manifestes), lecture ET écriture,
#     réutilise exactement le même token/la même authentification que
#     lib/huggingface.sh (déjà utilisé pour les poids H3) — aucune nouvelle
#     dépendance.
#   - GitHub Releases (PERSONAL_LORAS_GITHUB_RELEASE_URL) : socle de LoRAs
#     FIGÉS uniquement, lecture seule (jamais de push), aucune
#     authentification requise si le dépôt est public.
#
# Structure attendue à la racine du dépôt HF (type "dataset") :
#   loras/     -> ${INSTALL_DIR}/models/loras/personal/
#   presets/   -> ${PROJECT_ROOT}/presets/personal/
#   outputs/   -> ${INSTALL_DIR}/output/
#   workflows/ -> ${INSTALL_DIR}/user/default/workflows/personal/
#     (n'importe quels fichiers *.json de workflow ComfyUI que vous avez
#     vous-même exportés — copiés tels quels, aucun patch de nom de fichier
#     de modèle contrairement aux workflows officiels H3, cf.
#     lib/workflows.sh — c'est VOTRE workflow, configuré comme vous le
#     voulez.)
#
# MANIFESTES (fichiers texte à la racine du dépôt HF, un par ligne, lignes
# vides et commençant par '#' ignorées) — c'est la "configuration perso"
# proprement dite, déclarative : vous éditez ces fichiers directement sur
# huggingface.co (ou via `hf upload` depuis n'importe quelle machine), et
# CHAQUE pod/conteneur qui restaure ce coffre au démarrage applique le même
# souhait automatiquement, sans jamais avoir à ressaisir quoi que ce soit.
# Ni loras_manifest.txt ni nodes_manifest.txt ne sont jamais écrits par
# sync_personal_storage_push() : ce sont des souhaits que VOUS déclarez, pas
# un état capturé automatiquement depuis ce qui est installé localement.
#   - loras_manifest.txt : une URL de LoRA par ligne (HF/CivitAI/lien
#     direct), avec un second mot optionnel pour forcer le nom de fichier
#     local. Voir _personal_storage_process_manifest_loras ci-dessous.
#   - nodes_manifest.txt : une URL de dépôt Git de nœud custom par ligne,
#     avec un second mot optionnel "false" pour désactiver l'installation
#     de son requirements.txt. Voir _personal_storage_process_manifest_nodes
#     ci-dessous.
#
# Le sous-dossier "personal/" pour les LoRAs (plutôt que la racine
# models/loras/) est un choix délibéré : le Turbo LoRA officiel MiniMax H3
# (install_turbo_lora, lib/lora_auto.sh) est déposé directement dans
# models/loras/ — les séparer évite d'avoir à distinguer un fichier "perso"
# d'un fichier "officiel" par convention de nommage fragile, et ComfyUI
# scanne récursivement models/loras/ donc les LoRAs perso restent
# sélectionnables normalement dans l'interface. Même raisonnement pour
# presets/personal/ et pour user/default/workflows/personal/ : le contenu
# versionné dans ce repo (H3_PRESET_WORKFLOWS, config.env ; workflows/
# officiels, cf. lib/workflows.sh) ne doit jamais être confondu avec du
# contenu perso poussé/tiré depuis le coffre HF de l'utilisateur. Les nœuds
# custom (custom_nodes/), en revanche, n'ont PAS cette distinction : ce sont
# des extensions ComfyUI comme les autres, indiscernables de celles listées
# dans OPTIONAL_NODE_REPOS (config.env) une fois installées.
#
# No-op propre et silencieux si aucune des deux variables n'est renseignée :
# ne doit jamais bloquer une installation qui n'utilise pas cette
# fonctionnalité.

PERSONAL_STORAGE_LORAS_DIR() { echo "${INSTALL_DIR}/models/loras/personal"; }
PERSONAL_STORAGE_PRESETS_DIR() { echo "${PROJECT_ROOT}/presets/personal"; }
PERSONAL_STORAGE_OUTPUTS_DIR() { echo "${INSTALL_DIR}/output"; }
PERSONAL_STORAGE_WORKFLOWS_DIR() { echo "${INSTALL_DIR}/user/default/workflows/personal"; }
# Dossier alimenté par loras_manifest.txt (voir
# _personal_storage_pull_hf_manifest_loras ci-dessous) — délibérément séparé
# de PERSONAL_STORAGE_LORAS_DIR ci-dessus : ces LoRA sont retéléchargés
# depuis leur source d'origine à chaque démarrage (le manifeste est la seule
# source de vérité), les inclure dans le coffre HF poussé par
# sync_personal_storage_push() gaspillerait bande passante et stockage pour
# un contenu déjà récupérable ailleurs. Même chemin que
# install_lora.sh::MANIFEST_LORA_DIR — ne pas diverger.
PERSONAL_STORAGE_MANIFEST_LORAS_DIR() { echo "${INSTALL_DIR}/models/loras/manifest"; }

# _personal_storage_hf_ready
# Vérifie que le CLI HF est disponible et authentifié, sans jamais forcer une
# connexion interactive (contrairement à hf_login() dans lib/huggingface.sh,
# utilisée pour les poids H3 obligatoires) : un coffre perso non accessible
# ne doit produire qu'un avertissement, jamais bloquer install.sh/update.sh.
_personal_storage_hf_ready() {
  # Le venv ET le CLI HF (paquet huggingface_hub) sont garantis présents ici :
  # install.sh appelle sync_personal_storage_pull APRÈS install_extra_
  # requirements dans ses deux branches (voir le commentaire à cet endroit
  # dans install.sh) — un venv fraîchement créé mais encore vide (avant
  # l'installation des dépendances) ne suffit pas : detect_hf_cli()
  # ci-dessous ne trouverait ni 'hf' ni 'huggingface-cli' et cette fonction
  # échouerait quand même. Ce garde reste utile en défense : dans l'image
  # Docker pré-installée, venv + CLI HF sont déjà baked dans l'image (voir
  # docker-build-steps-heavy.sh), donc ce cas ne se présente normalement jamais ;
  # il ne sert que de filet de sécurité si cette fonction venait à être
  # appelée depuis un autre contexte à l'avenir.
  if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
    log_warn "$(t ps_venv_missing)"
    return 1
  fi

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  detect_hf_cli
  if [[ -z "$HF_CLI" ]]; then
    log_warn "$(t ps_no_hf_cli)"
    deactivate
    return 1
  fi
  local whoami_output
  if ! whoami_output="$("$HF_CLI" auth whoami 2>/dev/null || "$HF_CLI" whoami 2>/dev/null)" \
    || [[ -z "$whoami_output" ]] || [[ "$whoami_output" == *"Not logged in"* ]]; then
    log_warn "$(t ps_not_authenticated)"
    deactivate
    return 1
  fi
  deactivate
  return 0
}

# _personal_storage_process_manifest_loras <staging_dir>
# Traite loras_manifest.txt s'il est présent dans <staging_dir> (déjà
# téléchargé par le pull complet du coffre HF ci-dessous, à la racine du
# dépôt — pas de second appel réseau nécessaire ici) : une URL par ligne
# (Hugging Face/CivitAI/lien direct, mêmes sources que install_lora.sh),
# lignes vides et commençant par '#' ignorées. Un second mot optionnel sur
# la ligne impose le nom de fichier local (équivalent de --filename).
# Chaque URL est ensuite installée via install_lora.sh --manifest, dans
# PERSONAL_STORAGE_MANIFEST_LORAS_DIR — aucune logique de téléchargement
# dupliquée ici (mêmes garanties que l'installation manuelle : détection de
# nom, retries, validation .safetensors, authentification CivitAI via
# CIVITAI_API_KEY si définie).
#
# Best-effort et non bloquant, comme le reste de ce fichier : absence du
# manifeste (fonctionnalité non utilisée) ou échec d'une URL individuelle ne
# doit jamais interrompre sync_personal_storage_pull(). PERSONAL_STORAGE_
# MANIFEST_LORAS_FILENAME (config.env) permet de renommer ce fichier si
# besoin ; "loras_manifest.txt" par défaut.
_personal_storage_process_manifest_loras() {
  local staging="$1"
  local manifest_filename="${PERSONAL_STORAGE_MANIFEST_LORAS_FILENAME:-loras_manifest.txt}"
  local manifest_file="${staging}/${manifest_filename}"

  [[ -s "$manifest_file" ]] || return 0

  mkdir -p "$(PERSONAL_STORAGE_MANIFEST_LORAS_DIR)"

  local total=0 ok=0 failed=0
  local line url name
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<< "$line")"
    [[ -z "$line" || "$line" == \#* ]] && continue

    url="$(awk '{print $1}' <<< "$line")"
    name="$(awk '{print $2}' <<< "$line")"

    if [[ ! "$url" =~ ^https?:// ]]; then
      log_warn "$(t ps_manifest_invalid_line "$manifest_filename" "$line")"
      continue
    fi

    total=$((total + 1))
    local -a lora_args=(--manifest)
    [[ -n "$name" ]] && lora_args+=(--filename "$name")
    lora_args+=("$url")

    if bash "${PROJECT_ROOT}/install_lora.sh" "${lora_args[@]}" >>"$LOG_FILE" 2>&1; then
      ok=$((ok + 1))
    else
      failed=$((failed + 1))
      log_warn "$(t ps_manifest_lora_install_failed "$url" "$LOG_FILE")"
    fi
  done < "$manifest_file"

  if [[ "$total" -eq 0 ]]; then
    log_info "$(t ps_manifest_empty "$manifest_filename")"
  elif [[ "$failed" -eq 0 ]]; then
    log_ok "$(t ps_manifest_loras_ok "$ok" "$total" "$manifest_filename")"
  else
    log_warn "$(t ps_manifest_loras_partial "$ok" "$total" "$failed" "$manifest_filename" "$LOG_FILE")"
  fi
}

# _personal_storage_process_manifest_nodes <staging_dir>
# Traite nodes_manifest.txt s'il est présent dans <staging_dir> (même
# principe que _personal_storage_process_manifest_loras juste au-dessus,
# mais pour des dépôts de nœuds custom Git plutôt que des URLs de LoRA) :
# une URL de dépôt Git par ligne, lignes vides et commençant par '#'
# ignorées. Un second mot optionnel "false" désactive l'installation du
# requirements.txt du dépôt (équivalent de OPTIONAL_NODE_REPOS_NO_PIP,
# config.env) — "true" (installer les dépendances) par défaut.
#
# Chaque dépôt est cloné/mis à jour via _clone_or_update_node_repo
# (lib/nodes.sh) — EXACTEMENT la même fonction que pour OPTIONAL_NODE_REPOS,
# aucune logique de clonage dupliquée ici. Un nœud installé de cette façon
# atterrit dans ${INSTALL_DIR}/custom_nodes comme n'importe quel autre nœud
# optionnel : contrairement aux LoRA/presets/workflows, il n'y a pas de
# sous-dossier "personal" dédié (ce sont juste des extensions ComfyUI, pas
# des données personnelles à isoler).
#
# Best-effort et non bloquant, comme le reste de ce fichier : un échec de
# clonage individuel (déjà géré et loggué par _clone_or_update_node_repo
# elle-même) ne doit jamais interrompre le traitement du reste du manifeste.
# PERSONAL_STORAGE_MANIFEST_NODES_FILENAME (config.env) permet de renommer
# ce fichier si besoin ; "nodes_manifest.txt" par défaut.
_personal_storage_process_manifest_nodes() {
  local staging="$1"
  local manifest_filename="${PERSONAL_STORAGE_MANIFEST_NODES_FILENAME:-nodes_manifest.txt}"
  local manifest_file="${staging}/${manifest_filename}"

  [[ -s "$manifest_file" ]] || return 0

  mkdir -p "${INSTALL_DIR}/custom_nodes"

  local total=0
  local line url allow_pip
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<< "$line")"
    [[ -z "$line" || "$line" == \#* ]] && continue

    url="$(awk '{print $1}' <<< "$line")"
    allow_pip="$(awk '{print $2}' <<< "$line")"
    [[ "$allow_pip" == "false" ]] || allow_pip="true"

    if [[ ! "$url" =~ ^https?:// ]]; then
      log_warn "$(t ps_manifest_invalid_line "$manifest_filename" "$line")"
      continue
    fi

    total=$((total + 1))
    _clone_or_update_node_repo "$url" "$allow_pip"
  done < "$manifest_file"

  if [[ "$total" -eq 0 ]]; then
    log_info "$(t ps_manifest_empty "$manifest_filename")"
  else
    log_ok "$(t ps_manifest_nodes_ok "$total" "$manifest_filename")"
  fi
}

# sync_personal_storage_pull
# Rapatrie LoRAs/presets/outputs perso au démarrage d'un nouveau pod/
# conteneur, AVANT que ComfyUI ne soit lancé. Appelée depuis install.sh (tout
# début, après vérification des prérequis) et depuis docker-entrypoint.sh.
sync_personal_storage_pull() {
  log_step "$(t ps_pull_step)"

  if [[ -z "${PERSONAL_STORAGE_HF_REPO:-}" && -z "${PERSONAL_LORAS_GITHUB_RELEASE_URL:-}" ]]; then
    log_info "$(t ps_pull_disabled)"
    return 0
  fi

  mkdir -p "$(PERSONAL_STORAGE_LORAS_DIR)" "$(PERSONAL_STORAGE_PRESETS_DIR)" "$(PERSONAL_STORAGE_OUTPUTS_DIR)" "$(PERSONAL_STORAGE_WORKFLOWS_DIR)"

  if [[ -n "${PERSONAL_STORAGE_HF_REPO:-}" ]]; then
    if _personal_storage_hf_ready; then
      log_info "$(t ps_pull_hf_fetching "$PERSONAL_STORAGE_HF_REPO")"
      local staging
      staging="$(mktemp -d)"
      # shellcheck disable=SC1091
      source "${VENV_DIR}/bin/activate"
      if retry "$DOWNLOAD_MAX_RETRIES" "$HF_CLI" download "$PERSONAL_STORAGE_HF_REPO" \
          --repo-type dataset --local-dir "$staging" >>"$LOG_FILE" 2>&1; then
        # rsync si disponible (préserve mieux les métadonnées et gère les
        # suppressions distantes le cas échéant) ; repli sur cp -a sinon —
        # ni l'un ni l'autre n'écrase silencieusement des fichiers plus
        # récents localement que sur le coffre distant n'a pas d'importance
        # ici : le coffre HF est la référence "au démarrage d'un nouveau
        # conteneur", où le local est par construction vide/absent.
        if require_cmd rsync; then
          [[ -d "${staging}/loras" ]]     && rsync -a "${staging}/loras/"     "$(PERSONAL_STORAGE_LORAS_DIR)/"
          [[ -d "${staging}/presets" ]]   && rsync -a "${staging}/presets/"   "$(PERSONAL_STORAGE_PRESETS_DIR)/"
          [[ -d "${staging}/outputs" ]]   && rsync -a "${staging}/outputs/"   "$(PERSONAL_STORAGE_OUTPUTS_DIR)/"
          [[ -d "${staging}/workflows" ]] && rsync -a "${staging}/workflows/" "$(PERSONAL_STORAGE_WORKFLOWS_DIR)/"
        else
          [[ -d "${staging}/loras" ]]     && cp -a "${staging}/loras/."     "$(PERSONAL_STORAGE_LORAS_DIR)/"
          [[ -d "${staging}/presets" ]]   && cp -a "${staging}/presets/."   "$(PERSONAL_STORAGE_PRESETS_DIR)/"
          [[ -d "${staging}/outputs" ]]   && cp -a "${staging}/outputs/."   "$(PERSONAL_STORAGE_OUTPUTS_DIR)/"
          [[ -d "${staging}/workflows" ]] && cp -a "${staging}/workflows/." "$(PERSONAL_STORAGE_WORKFLOWS_DIR)/"
        fi
        log_ok "$(t ps_pull_hf_restored "$PERSONAL_STORAGE_HF_REPO")"

        # Manifestes déclaratifs (loras_manifest.txt / nodes_manifest.txt à
        # la racine du dépôt HF, déjà présents dans $staging — aucun appel
        # réseau supplémentaire) — voir le commentaire d'en-tête de ce
        # fichier (section MANIFESTES) et celui de chaque fonction pour le
        # détail. Appelés ici, dans le bloc encore activé du venv (le CLI
        # HF et pip_install_requirements en ont besoin), avant `deactivate`.
        _personal_storage_process_manifest_loras "$staging"
        _personal_storage_process_manifest_nodes "$staging"
      else
        log_warn "$(t ps_pull_hf_failed "$PERSONAL_STORAGE_HF_REPO" "$LOG_FILE")"
      fi
      deactivate
      rm -rf "$staging"
    fi
  fi

  if [[ -n "${PERSONAL_LORAS_GITHUB_RELEASE_URL:-}" ]]; then
    _personal_storage_pull_github_release_loras
  fi

  log_ok "$(t ps_pull_done)"
}

# _personal_storage_pull_github_release_loras
# Télécharge les assets de la release GitHub PERSONAL_LORAS_GITHUB_RELEASE_URL
# (URL de la PAGE de release, ex: https://github.com/<user>/<repo>/releases/tag/<tag>)
# dans models/loras/personal/. Aucune authentification requise (dépôt public
# attendu) : simple appel à l'API GitHub (releases) pour lister les assets,
# puis `curl -L` par asset — jamais de dépendance à `gh` (CLI GitHub non
# installé par ce projet). Skip silencieux si un fichier de même nom est déjà
# présent (même logique "déjà présent" que lib/models.sh pour les poids H3).
_personal_storage_pull_github_release_loras() {
  local url="${PERSONAL_LORAS_GITHUB_RELEASE_URL}"

  if ! require_cmd curl; then
    log_warn "$(t ps_gh_no_curl)"
    return 0
  fi

  # https://github.com/<owner>/<repo>/releases/tag/<tag>  ->  owner, repo, tag
  local owner repo tag
  if [[ "$url" =~ github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+) ]]; then
    owner="${BASH_REMATCH[1]}"
    repo="${BASH_REMATCH[2]}"
    tag="${BASH_REMATCH[3]}"
  else
    log_warn "$(t ps_gh_bad_url)"
    return 0
  fi

  log_info "$(t ps_gh_fetching "$owner" "$repo" "$tag")"

  local api_url="https://api.github.com/repos/${owner}/${repo}/releases/tags/${tag}"
  local assets_json
  if ! assets_json="$(curl -fsSL "$api_url" 2>>"$LOG_FILE")"; then
    log_warn "$(t ps_gh_api_unreachable "$api_url")"
    return 0
  fi

  # Extraction minimale nom+URL sans dépendance à jq (non garanti présent) :
  # les deux champs sont sur des lignes séparées dans le JSON formaté par
  # l'API GitHub, on les recompose par paire dans l'ordre d'apparition.
  local names urls
  names="$(grep -o '"name": *"[^"]*"' <<< "$assets_json" | sed 's/.*: *"//;s/"$//')"
  urls="$(grep -o '"browser_download_url": *"[^"]*"' <<< "$assets_json" | sed 's/.*: *"//;s/"$//')"

  if [[ -z "$urls" ]]; then
    log_warn "$(t ps_gh_no_assets "$owner" "$repo" "$tag")"
    return 0
  fi

  local dest_dir; dest_dir="$(PERSONAL_STORAGE_LORAS_DIR)"
  local name url_i i=0
  while IFS= read -r url_i; do
    i=$((i + 1))
    name="$(sed -n "${i}p" <<< "$names")"
    [[ -n "$name" ]] || name="$(basename "$url_i")"
    if [[ -f "${dest_dir}/${name}" ]]; then
      log_ok "$(t ps_gh_already_present "$name")"
      continue
    fi
    announce_download "$name"
    if ! retry "$DOWNLOAD_MAX_RETRIES" curl -fL -o "${dest_dir}/${name}.part" "$url_i" >>"$LOG_FILE" 2>&1; then
      log_warn "$(t ps_gh_download_failed "$name" "$LOG_FILE")"
      rm -f "${dest_dir}/${name}.part"
      continue
    fi
    mv "${dest_dir}/${name}.part" "${dest_dir}/${name}"
    log_ok "$(t ps_gh_downloaded "$name")"
  done <<< "$urls"
}

# sync_personal_storage_push
# Repousse LoRAs/presets/outputs perso vers le coffre HF, typiquement en fin
# de session (avant de terminate un pod) — voir sync_push.sh à la racine du
# projet pour un lancement manuel, et update.sh qui l'appelle aussi en fin de
# mise à jour. Le backend GitHub Releases est volontairement absent ici :
# c'est un socle de LoRAs figés, lecture seule (voir commentaire
# PERSONAL_LORAS_GITHUB_RELEASE_URL dans config.env) — on ne pousse jamais
# dessus depuis ce projet.
# _personal_storage_hf_upload <dossier_local> <chemin_dans_le_repo>
# Enrobe `hf upload` avec la même logique de tentatives que retry()
# (lib/utils.sh), SAUF pour une erreur 403 (permission refusée) : celle-ci
# ne se résout jamais toute seule en réessayant (token en lecture seule, ou
# sans accès en écriture au dépôt) — retenter 5 fois ne fait que perdre du
# temps et polluer les logs. On détecte ce cas précis dans la sortie de `hf`
# et on abandonne immédiatement avec un message actionnable.
# Retourne : 0 succès, 1 échec (épuisement des tentatives), 2 échec
# permanent (403 — inutile de réessayer, y compris pour les autres dossiers
# à pousser : voir l'appelant, sync_personal_storage_push()).
_personal_storage_hf_upload() {
  local local_dir="$1" path_in_repo="$2"
  local attempt=1 out

  while (( attempt <= DOWNLOAD_MAX_RETRIES )); do
    if out="$("$HF_CLI" upload "$PERSONAL_STORAGE_HF_REPO" "$local_dir" "$path_in_repo" --repo-type dataset 2>&1)"; then
      echo "$out" >> "$LOG_FILE"
      return 0
    fi

    echo "$out" >> "$LOG_FILE"

    if grep -qi '403 Forbidden\|correct permissions' <<< "$out"; then
      log_error "$(t ps_upload_403 "$PERSONAL_STORAGE_HF_REPO" "$path_in_repo")"
      log_error "$(t ps_upload_403_fix)"
      return 2
    fi

    if (( attempt >= DOWNLOAD_MAX_RETRIES )); then
      log_error "$(t ps_upload_failed "$DOWNLOAD_MAX_RETRIES" "$PERSONAL_STORAGE_HF_REPO" "$local_dir" "$path_in_repo")"
      return 1
    fi

    log_warn "$(t retry_attempt "$attempt" "$DOWNLOAD_MAX_RETRIES")"
    sleep 5
    ((attempt++))
  done
}

# _personal_storage_warn_non_personal_loras
# Avertit (log_warn, non bloquant) si des LoRA existent dans models/loras/
# (racine, PAS models/loras/personal/) au moment d'un push — ce dossier
# n'est jamais sauvegardé par sync_personal_storage_push() (voir le
# commentaire d'en-tête de ce fichier), donc un LoRA installé via
# `install_lora.sh <URL>` sans --personal y reste invisible pour ce
# mécanisme sans aucun message d'erreur nulle part, ce qui peut faire
# croire à tort que "la sauvegarde perso ne fonctionne pas pour les LoRA"
# alors qu'elle fonctionne exactement comme prévu, juste pas sur ce
# dossier. Le Turbo LoRA officiel MiniMax H3 (MINIMAX_H3_TURBO_LORA_FILENAME,
# config.env) est délibérément exclu de cet avertissement : il atterrit là
# par conception (lib/lora_auto.sh) et est retéléchargeable à volonté depuis
# Hugging Face — il n'a jamais eu vocation à être sauvegardé perso.
_personal_storage_warn_non_personal_loras() {
  local root_dir="${INSTALL_DIR}/models/loras"
  [[ -d "$root_dir" ]] || return 0

  local turbo_name="${MINIMAX_H3_TURBO_LORA_FILENAME:-}"
  local -a stray=()
  local f name
  while IFS= read -r -d '' f; do
    name="$(basename "$f")"
    [[ -n "$turbo_name" && "$name" == "$turbo_name" ]] && continue
    stray+=("$name")
  done < <(find "$root_dir" -maxdepth 1 -type f -name '*.safetensors' -print0 2>/dev/null)

  if [[ ${#stray[@]} -gt 0 ]]; then
    log_warn "$(t ps_stray_loras_warn "${#stray[@]}" "$root_dir" "$root_dir")"
    for name in "${stray[@]}"; do
      log_warn "    - ${name}"
    done
    log_warn "$(t ps_stray_loras_fix "$root_dir" "$root_dir")"
  fi
}

sync_personal_storage_push() {
  log_step "$(t ps_push_step)"

  if [[ -z "${PERSONAL_STORAGE_HF_REPO:-}" ]]; then
    log_info "$(t ps_push_disabled)"
    return 0
  fi

  _personal_storage_hf_ready || return 0

  mkdir -p "$(PERSONAL_STORAGE_LORAS_DIR)" "$(PERSONAL_STORAGE_PRESETS_DIR)" "$(PERSONAL_STORAGE_OUTPUTS_DIR)"

  _personal_storage_warn_non_personal_loras

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  local ok="true" rc
  log_info "$(t ps_push_uploading "$PERSONAL_STORAGE_HF_REPO")"

  if [[ -n "$(ls -A "$(PERSONAL_STORAGE_LORAS_DIR)" 2>/dev/null)" ]]; then
    _personal_storage_hf_upload "$(PERSONAL_STORAGE_LORAS_DIR)" loras; rc=$?
    [[ "$rc" -ne 0 ]] && ok="false"
  fi
  # rc=2 (403) : même token, même dépôt pour les trois — inutile de retenter
  # presets/outputs, l'erreur sera strictement identique. On s'arrête net.
  if [[ "$ok" == "true" || "${rc:-0}" -ne 2 ]]; then
    if [[ -n "$(ls -A "$(PERSONAL_STORAGE_PRESETS_DIR)" 2>/dev/null)" ]]; then
      _personal_storage_hf_upload "$(PERSONAL_STORAGE_PRESETS_DIR)" presets; rc=$?
      [[ "$rc" -ne 0 ]] && ok="false"
    fi
  fi
  if [[ "$ok" == "true" || "${rc:-0}" -ne 2 ]]; then
    if [[ -n "$(ls -A "$(PERSONAL_STORAGE_OUTPUTS_DIR)" 2>/dev/null)" ]]; then
      _personal_storage_hf_upload "$(PERSONAL_STORAGE_OUTPUTS_DIR)" outputs; rc=$?
      [[ "$rc" -ne 0 ]] && ok="false"
    fi
  fi
  deactivate

  if [[ "$ok" == "true" ]]; then
    log_ok "$(t ps_push_done "$PERSONAL_STORAGE_HF_REPO")"
  elif [[ "${rc:-0}" -eq 2 ]]; then
    log_warn "$(t ps_push_403)"
  else
    log_warn "$(t ps_push_incomplete "$LOG_FILE")"
  fi
}
