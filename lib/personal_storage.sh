#!/usr/bin/env bash
# lib/personal_storage.sh — sauvegarde/restauration des LoRAs, presets et
# outputs perso, indépendamment du Network Volume RunPod (payant, lié à un
# seul datacenter — voir la note d'objectif dans config.env).
#
# Deux backends indépendants, qui peuvent cohabiter :
#   - Hugging Face (PERSONAL_STORAGE_HF_REPO) : coffre privé complet
#     (loras/presets/outputs), lecture ET écriture, réutilise exactement le
#     même token/la même authentification que lib/huggingface.sh (déjà
#     utilisé pour les poids H3) — aucune nouvelle dépendance.
#   - GitHub Releases (PERSONAL_LORAS_GITHUB_RELEASE_URL) : socle de LoRAs
#     FIGÉS uniquement, lecture seule (jamais de push), aucune
#     authentification requise si le dépôt est public.
#
# Structure attendue à la racine du dépôt HF (type "dataset") :
#   loras/     -> ${INSTALL_DIR}/models/loras/personal/
#   presets/   -> ${PROJECT_ROOT}/presets/personal/
#   outputs/   -> ${INSTALL_DIR}/output/
#
# Le sous-dossier "personal/" pour les LoRAs (plutôt que la racine
# models/loras/) est un choix délibéré : le Turbo LoRA officiel MiniMax H3
# (install_turbo_lora, lib/lora_auto.sh) est déposé directement dans
# models/loras/ — les séparer évite d'avoir à distinguer un fichier "perso"
# d'un fichier "officiel" par convention de nommage fragile, et ComfyUI
# scanne récursivement models/loras/ donc les LoRAs perso restent
# sélectionnables normalement dans l'interface. Même raisonnement pour
# presets/personal/ : les presets versionnés dans le repo (voir
# H3_PRESET_WORKFLOWS, config.env) ne doivent jamais être confondus avec un
# preset perso poussé/tiré depuis le coffre HF de l'utilisateur.
#
# No-op propre et silencieux si aucune des deux variables n'est renseignée :
# ne doit jamais bloquer une installation qui n'utilise pas cette
# fonctionnalité.

PERSONAL_STORAGE_LORAS_DIR() { echo "${INSTALL_DIR}/models/loras/personal"; }
PERSONAL_STORAGE_PRESETS_DIR() { echo "${PROJECT_ROOT}/presets/personal"; }
PERSONAL_STORAGE_OUTPUTS_DIR() { echo "${INSTALL_DIR}/output"; }

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
  # docker-build-steps.sh), donc ce cas ne se présente normalement jamais ;
  # il ne sert que de filet de sécurité si cette fonction venait à être
  # appelée depuis un autre contexte à l'avenir.
  if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
    log_warn "Environnement virtuel pas encore créé — synchronisation HF du stockage perso reportée (sera retentée par update.sh ou un futur redémarrage)."
    return 1
  fi

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  detect_hf_cli
  if [[ -z "$HF_CLI" ]]; then
    log_warn "Ni 'hf' ni 'huggingface-cli' disponibles dans le venv — synchronisation HF du stockage perso sautée."
    deactivate
    return 1
  fi
  local whoami_output
  if ! whoami_output="$("$HF_CLI" auth whoami 2>/dev/null || "$HF_CLI" whoami 2>/dev/null)" \
    || [[ -z "$whoami_output" ]] || [[ "$whoami_output" == *"Not logged in"* ]]; then
    log_warn "Non authentifié à Hugging Face — synchronisation du stockage perso sautée (lancez 'bash install.sh' au moins une fois pour vous authentifier, ou définissez HF_TOKEN)."
    deactivate
    return 1
  fi
  deactivate
  return 0
}

# sync_personal_storage_pull
# Rapatrie LoRAs/presets/outputs perso au démarrage d'un nouveau pod/
# conteneur, AVANT que ComfyUI ne soit lancé. Appelée depuis install.sh (tout
# début, après vérification des prérequis) et depuis docker-entrypoint.sh.
sync_personal_storage_pull() {
  log_step "Stockage perso (LoRAs/presets/outputs) — restauration"

  if [[ -z "${PERSONAL_STORAGE_HF_REPO:-}" && -z "${PERSONAL_LORAS_GITHUB_RELEASE_URL:-}" ]]; then
    log_info "PERSONAL_STORAGE_HF_REPO et PERSONAL_LORAS_GITHUB_RELEASE_URL vides — fonctionnalité désactivée, étape sautée."
    return 0
  fi

  mkdir -p "$(PERSONAL_STORAGE_LORAS_DIR)" "$(PERSONAL_STORAGE_PRESETS_DIR)" "$(PERSONAL_STORAGE_OUTPUTS_DIR)"

  if [[ -n "${PERSONAL_STORAGE_HF_REPO:-}" ]]; then
    if _personal_storage_hf_ready; then
      log_info "Récupération du coffre HF ${PERSONAL_STORAGE_HF_REPO}..."
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
          [[ -d "${staging}/loras" ]]   && rsync -a "${staging}/loras/"   "$(PERSONAL_STORAGE_LORAS_DIR)/"
          [[ -d "${staging}/presets" ]] && rsync -a "${staging}/presets/" "$(PERSONAL_STORAGE_PRESETS_DIR)/"
          [[ -d "${staging}/outputs" ]] && rsync -a "${staging}/outputs/" "$(PERSONAL_STORAGE_OUTPUTS_DIR)/"
        else
          [[ -d "${staging}/loras" ]]   && cp -a "${staging}/loras/."   "$(PERSONAL_STORAGE_LORAS_DIR)/"
          [[ -d "${staging}/presets" ]] && cp -a "${staging}/presets/." "$(PERSONAL_STORAGE_PRESETS_DIR)/"
          [[ -d "${staging}/outputs" ]] && cp -a "${staging}/outputs/." "$(PERSONAL_STORAGE_OUTPUTS_DIR)/"
        fi
        log_ok "Coffre HF restauré (${PERSONAL_STORAGE_HF_REPO})."
      else
        log_warn "Échec de récupération du coffre HF ${PERSONAL_STORAGE_HF_REPO} (consultez ${LOG_FILE}) — étape non bloquante, on continue."
      fi
      deactivate
      rm -rf "$staging"
    fi
  fi

  if [[ -n "${PERSONAL_LORAS_GITHUB_RELEASE_URL:-}" ]]; then
    _personal_storage_pull_github_release_loras
  fi

  log_ok "Restauration du stockage perso terminée."
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
    log_warn "curl introuvable — récupération des LoRAs figés (GitHub Releases) sautée."
    return 0
  fi

  # https://github.com/<owner>/<repo>/releases/tag/<tag>  ->  owner, repo, tag
  local owner repo tag
  if [[ "$url" =~ github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+) ]]; then
    owner="${BASH_REMATCH[1]}"
    repo="${BASH_REMATCH[2]}"
    tag="${BASH_REMATCH[3]}"
  else
    log_warn "PERSONAL_LORAS_GITHUB_RELEASE_URL mal formée (attendu : https://github.com/<owner>/<repo>/releases/tag/<tag>) — étape sautée."
    return 0
  fi

  log_info "Récupération des LoRAs figés depuis la release GitHub ${owner}/${repo}@${tag}..."

  local api_url="https://api.github.com/repos/${owner}/${repo}/releases/tags/${tag}"
  local assets_json
  if ! assets_json="$(curl -fsSL "$api_url" 2>>"$LOG_FILE")"; then
    log_warn "Impossible de contacter l'API GitHub (${api_url}) — étape non bloquante, LoRAs figés sautés."
    return 0
  fi

  # Extraction minimale nom+URL sans dépendance à jq (non garanti présent) :
  # les deux champs sont sur des lignes séparées dans le JSON formaté par
  # l'API GitHub, on les recompose par paire dans l'ordre d'apparition.
  local names urls
  names="$(grep -o '"name": *"[^"]*"' <<< "$assets_json" | sed 's/.*: *"//;s/"$//')"
  urls="$(grep -o '"browser_download_url": *"[^"]*"' <<< "$assets_json" | sed 's/.*: *"//;s/"$//')"

  if [[ -z "$urls" ]]; then
    log_warn "Aucun asset trouvé sur la release ${owner}/${repo}@${tag} — rien à télécharger."
    return 0
  fi

  local dest_dir; dest_dir="$(PERSONAL_STORAGE_LORAS_DIR)"
  local name url_i i=0
  while IFS= read -r url_i; do
    i=$((i + 1))
    name="$(sed -n "${i}p" <<< "$names")"
    [[ -n "$name" ]] || name="$(basename "$url_i")"
    if [[ -f "${dest_dir}/${name}" ]]; then
      log_ok "${name} déjà présent — téléchargement sauté."
      continue
    fi
    announce_download "$name"
    if ! retry "$DOWNLOAD_MAX_RETRIES" curl -fL -o "${dest_dir}/${name}.part" "$url_i" >>"$LOG_FILE" 2>&1; then
      log_warn "Échec du téléchargement de ${name} (non bloquant, consultez ${LOG_FILE})."
      rm -f "${dest_dir}/${name}.part"
      continue
    fi
    mv "${dest_dir}/${name}.part" "${dest_dir}/${name}"
    log_ok "${name} téléchargé."
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
      log_error "Permission refusée par Hugging Face (403) sur ${PERSONAL_STORAGE_HF_REPO}/${path_in_repo} — votre HF_TOKEN n'a probablement pas les droits d'écriture."
      log_error "Créez un token avec la permission \"Write\" sur https://huggingface.co/settings/tokens, mettez à jour HF_TOKEN, puis relancez bash sync_push.sh."
      return 2
    fi

    if (( attempt >= DOWNLOAD_MAX_RETRIES )); then
      log_error "Échec après ${DOWNLOAD_MAX_RETRIES} tentatives : hf upload ${PERSONAL_STORAGE_HF_REPO} ${local_dir} ${path_in_repo} --repo-type dataset"
      return 1
    fi

    log_warn "Tentative ${attempt}/${DOWNLOAD_MAX_RETRIES} échouée, nouvelle tentative dans 5s..."
    sleep 5
    ((attempt++))
  done
}

sync_personal_storage_push() {
  log_step "Stockage perso (LoRAs/presets/outputs) — sauvegarde"

  if [[ -z "${PERSONAL_STORAGE_HF_REPO:-}" ]]; then
    log_info "PERSONAL_STORAGE_HF_REPO vide — sauvegarde HF désactivée, étape sautée."
    return 0
  fi

  _personal_storage_hf_ready || return 0

  mkdir -p "$(PERSONAL_STORAGE_LORAS_DIR)" "$(PERSONAL_STORAGE_PRESETS_DIR)" "$(PERSONAL_STORAGE_OUTPUTS_DIR)"

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  local ok="true" rc
  log_info "Envoi vers le coffre HF ${PERSONAL_STORAGE_HF_REPO} (peut prendre du temps selon le volume d'outputs)..."

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
    log_ok "Sauvegarde du stockage perso terminée (${PERSONAL_STORAGE_HF_REPO})."
  elif [[ "${rc:-0}" -eq 2 ]]; then
    log_warn "Sauvegarde du stockage perso interrompue (permission refusée) — corrigez HF_TOKEN puis relancez 'bash sync_push.sh'."
  else
    log_warn "Sauvegarde du stockage perso incomplète — consultez ${LOG_FILE}. Relancez 'bash sync_push.sh' pour réessayer."
  fi
}
