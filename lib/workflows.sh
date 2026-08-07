#!/usr/bin/env bash
# lib/workflows.sh — installation des workflows officiels MiniMax H3 dans
# ComfyUI.
#
# Copie le contenu de workflows/ (à la racine du projet) vers
# ${INSTALL_DIR}/user/default/workflows, c'est-à-dire l'emplacement où
# ComfyUI va lire les workflows à afficher dans son interface — mais
# uniquement les fichiers dont TOUS les modèles référencés sont eux-mêmes
# requis par la sélection H3_WORKFLOWS courante (sinon un workflow apparaît
# dans ComfyUI pour un modèle jamais téléchargé, cf. resolve_h3_workflows()
# dans lib/models.sh).
#
# La détection reste générique et ne duplique pas la logique de résolution
# des workflows : resolve_h3_workflows()/_workflow_needs() (lib/models.sh),
# déjà utilisées par build_h3_model_manifest()/download_h3_models()/
# verify_installation(), restent l'unique source de vérité pour "quels
# modèles sont requis par la sélection courante". Ce fichier se contente de
# lire, PAR CONTENU, quels modèles chaque *.json référence (même convention
# de préfixe de nom de fichier que lib/verify.sh :
# "minimax_h3_fl2va_"/"minimax_h3_ref2va_") — jamais par nom de fichier
# workflow. Un nouveau workflow déposé dans workflows/ (y compris dans des
# sous-dossiers) est donc géré automatiquement, sans modifier ce script.

install_workflows() {
  local src="${PROJECT_ROOT}/workflows"
  local dest="${INSTALL_DIR}/user/default/workflows"

  echo "------------------------------------------------"
  echo "Installing official MiniMax H3 workflows..."

  if [[ ! -d "$src" ]]; then
    log_warn "Dossier ${src} introuvable — aucun workflow à installer."
    echo "------------------------------------------------"
    return 0
  fi

  mkdir -p "$dest"

  # Modèles réellement disponibles pour la sélection H3_WORKFLOWS courante —
  # dérivés via _workflow_needs(), pas redéfinis ici.
  local workflows; workflows="$(resolve_h3_workflows)"
  local need_fl2va="false" need_ref2va="false"
  _workflow_needs "$workflows" t2v i2v && need_fl2va="true"
  _workflow_needs "$workflows" r2v && need_ref2va="true"

  local copied=() skipped=()
  local file rel target refs_fl2va refs_ref2va
  while IFS= read -r -d '' file; do
    rel="${file#"$src"/}"

    refs_fl2va="false"; refs_ref2va="false"
    grep -q "minimax_h3_fl2va_" "$file" 2>/dev/null && refs_fl2va="true"
    grep -q "minimax_h3_ref2va_" "$file" 2>/dev/null && refs_ref2va="true"

    if { [[ "$refs_fl2va" == "true" ]] && [[ "$need_fl2va" == "false" ]]; } || \
       { [[ "$refs_ref2va" == "true" ]] && [[ "$need_ref2va" == "false" ]]; }; then
      skipped+=("$rel")
      continue
    fi

    target="${dest}/${rel}"
    mkdir -p "$(dirname "$target")"
    cp -f "$file" "$target"
    copied+=("$rel")
  done < <(find "$src" -type f -iname "*.json" -print0 | sort -z)

  if (( ${#skipped[@]} > 0 )); then
    echo "Skipped (require a model outside the selected workflows '${workflows}'):"
    for rel in "${skipped[@]}"; do
      echo "- ${rel}"
    done
  fi

  if (( ${#copied[@]} == 0 )); then
    log_warn "Aucun workflow compatible avec H3_WORKFLOWS='${workflows}' trouvé dans ${src} — rien à installer."
    echo "------------------------------------------------"
    return 0
  fi

  echo "Copied:"
  for rel in "${copied[@]}"; do
    echo "- ${rel}"
  done

  if ! _verify_workflows_installed "${copied[@]}"; then
    echo "------------------------------------------------"
    return 1
  fi

  echo "Official workflows installed successfully."
  echo "------------------------------------------------"

  log_ok "Workflows installés dans ${dest} (${#copied[@]})."
}

# _verify_workflows_installed <chemin_relatif...>
# Vérifie que chaque fichier copié est bien présent dans le dossier de
# destination. Affiche une erreur claire et retourne 1 si l'un d'eux manque.
_verify_workflows_installed() {
  local dest="${INSTALL_DIR}/user/default/workflows"
  local rel missing=0

  for rel in "$@"; do
    if [[ ! -f "${dest}/${rel}" ]]; then
      log_error "Workflow manquant après copie : ${rel}"
      missing=1
    fi
  done

  if (( missing == 1 )); then
    log_error "Un ou plusieurs workflows n'ont pas été installés correctement dans ${dest}."
    return 1
  fi

  return 0
}
