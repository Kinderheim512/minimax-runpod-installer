#!/usr/bin/env bash
# lib/workflows.sh — installation des workflows officiels MiniMax H3 dans
# ComfyUI.
#
# Copie tout le contenu de workflows/ (à la racine du projet) vers
# ${INSTALL_DIR}/user/default/workflows, c'est-à-dire l'emplacement où
# ComfyUI va lire les workflows à afficher dans son interface. La copie est
# générique : tout fichier *.json présent dans workflows/ (y compris dans des
# sous-dossiers) est copié, sans qu'il soit nécessaire de modifier ce script
# si de nouveaux workflows sont ajoutés au dépôt plus tard.

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

  local copied=()
  local file rel target
  while IFS= read -r -d '' file; do
    rel="${file#"$src"/}"
    target="${dest}/${rel}"
    mkdir -p "$(dirname "$target")"
    cp -f "$file" "$target"
    copied+=("$rel")
  done < <(find "$src" -type f -iname "*.json" -print0 | sort -z)

  if (( ${#copied[@]} == 0 )); then
    log_warn "Aucun fichier .json trouvé dans ${src} — rien à installer."
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
