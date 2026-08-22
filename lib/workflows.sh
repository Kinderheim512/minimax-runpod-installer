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
# dans lib/models.sh). Chaque copie est ensuite adaptée au palier H3_TIER
# sélectionné (voir _patch_workflow_tier_filenames ci-dessous), pour qu'un
# workflow installé fonctionne immédiatement sans resélection manuelle du
# modèle dans l'interface ComfyUI.
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
  echo "$(t wf_installing_header)"

  if [[ ! -d "$src" ]]; then
    log_warn "$(t wf_src_missing "$src")"
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

  # Palier courant — résolu ici (pas supposé déjà peuplé) car cette fonction
  # peut s'exécuter sans téléchargement préalable (ex : install.sh
  # --skip-models). build_h3_model_manifest() est la même fonction utilisée
  # par download_h3_models() : H3_MODEL_FILES reste l'unique source de
  # vérité pour "quel fichier pour quel palier", jamais redéfinie ici.
  local tier; tier="$(resolve_h3_tier)"
  build_h3_model_manifest "$tier"

  local copied=() skipped=() patched=()
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

    if _patch_workflow_tier_filenames "$target" "$tier"; then
      patched+=("$rel")
    fi
  done < <(find "$src" -type f -iname "*.json" -print0 | sort -z)

  if (( ${#skipped[@]} > 0 )); then
    echo "$(t wf_skipped_header "$workflows")"
    for rel in "${skipped[@]}"; do
      echo "- ${rel}"
    done
  fi

  if (( ${#copied[@]} == 0 )); then
    log_warn "$(t wf_none_compatible "$workflows" "$src")"
    echo "------------------------------------------------"
    return 0
  fi

  echo "$(t wf_copied_header)"
  for rel in "${copied[@]}"; do
    echo "- ${rel}"
  done

  if (( ${#patched[@]} > 0 )); then
    echo "$(t wf_adapted_header "$tier")"
    for rel in "${patched[@]}"; do
      echo "- ${rel}"
    done
  fi

  if ! _verify_workflows_installed "${copied[@]}"; then
    echo "------------------------------------------------"
    return 1
  fi

  _warn_stale_tier_filenames "${copied[@]}"

  echo "$(t wf_success_footer)"
  echo "------------------------------------------------"

  log_ok "$(t wf_installed_ok "$dest" "${#copied[@]}" "$tier")"
}

# _known_filenames_for_key <key>
# Liste (une par ligne) les noms de fichier des TROIS paliers pour une clé
# de modèle (fl2va / ref2va / text_encoder), à partir des mêmes tableaux
# H3_DIFFUSION_FL2VA / H3_DIFFUSION_REF2VA / H3_TEXT_ENCODER que
# build_h3_model_manifest() (lib/models.sh) — aucune liste de noms
# dupliquée ici.
_known_filenames_for_key() {
  local key="$1"
  local arr_name=""
  case "$key" in
    fl2va)         arr_name="H3_DIFFUSION_FL2VA" ;;
    ref2va)        arr_name="H3_DIFFUSION_REF2VA" ;;
    text_encoder)  arr_name="H3_TEXT_ENCODER" ;;
    *) return 1 ;;
  esac
  local -n arr="$arr_name"
  local entry subpath
  for entry in "${arr[@]}"; do
    # Format "repo|sous_chemin|palier|taille" (4 champs, architecture
    # multi-dépôts — voir H3_DIFFUSION_FL2VA dans lib/models.sh) : le champ
    # à extraire est le 2e, pas le 1er. `read -r subpath _ _` (3
    # placeholders) lisait par erreur le champ "repo" dans $subpath — bug
    # découvert en vérifiant le passage à 5 paliers, présent depuis
    # l'introduction du champ "repo" dans le manifeste.
    IFS='|' read -r _ subpath _ _ <<< "$entry"
    basename "$subpath"
  done
}

# _patch_workflow_tier_filenames <fichier_copié> <palier>
# Réécrit, DANS LA COPIE installée (jamais dans workflows/ source, qui reste
# intact dans le dépôt), les noms de fichier de modèle de diffusion et de
# text encoder référencés par les nœuds du workflow, pour qu'ils
# correspondent au palier réellement sélectionné plutôt qu'au palier que le
# template officiel a codé en dur.
#
# Fonctionne par remplacement de texte brut (nom de fichier exact, unique et
# sans caractère spécial JSON), pas par analyse des `widgets_values` : ces
# tableaux sont positionnels et propres à chaque type de nœud, donc les
# parser prend le risque de casser silencieusement au prochain changement de
# schéma côté ComfyUI/Comfy-Org. Un remplacement textuel du nom de fichier
# est agnostique du nœud qui le contient (loader, note Markdown
# documentaire, etc.) et corrige donc aussi la documentation embarquée du
# workflow au passage.
#
# Cherche indifféremment le nom de N'IMPORTE LEQUEL des trois paliers connus
# (pas seulement "light") : les templates officiels codent actuellement en
# dur le palier light, mais si Comfy-Org change un jour ce choix par
# défaut, cette fonction continue de fonctionner sans modification.
#
# Retourne 0 (et n'affiche rien) si au moins un remplacement a eu lieu,
# 1 sinon — utilisé par l'appelant pour ne lister que les fichiers
# réellement modifiés.
_patch_workflow_tier_filenames() {
  local file="$1" tier="$2"
  local key target_subpath target_name candidate changed="false"

  for key in fl2va ref2va text_encoder; do
    target_subpath="${H3_MODEL_FILES[$key]:-}"
    [[ -z "$target_subpath" ]] && continue
    target_name="$(basename "$target_subpath")"

    while IFS= read -r candidate; do
      [[ -z "$candidate" || "$candidate" == "$target_name" ]] && continue
      if grep -qF -- "$candidate" "$file" 2>/dev/null; then
        # candidate/target_name are plain filenames (letters, digits, '_',
        # '.') : jamais de caractère spécial pour sed, un remplacement
        # littéral simple suffit.
        sed -i "s/${candidate}/${target_name}/g" "$file"
        changed="true"
      fi
    done < <(_known_filenames_for_key "$key")
  done

  [[ "$changed" == "true" ]]
}

# _warn_stale_tier_filenames <chemin_relatif...>
# Filet de sécurité : après adaptation, vérifie qu'aucun nom de fichier d'un
# AUTRE palier ne subsiste dans les workflows copiés. Ne devrait jamais se
# déclencher avec les templates actuels — sert à détecter tôt une
# convention de nommage future que _known_filenames_for_key() ne
# reconnaîtrait pas encore, plutôt que de laisser l'utilisateur découvrir un
# loader mal résolu dans l'interface ComfyUI. Non bloquant (avertissement
# seul) : voir TROUBLESHOOTING.md pour la procédure de repli manuelle.
_warn_stale_tier_filenames() {
  local dest="${INSTALL_DIR}/user/default/workflows"
  local rel key candidate target_name found_any="false"

  for rel in "$@"; do
    for key in fl2va ref2va text_encoder; do
      target_name="$(basename "${H3_MODEL_FILES[$key]:-}")"
      [[ -z "$target_name" ]] && continue
      while IFS= read -r candidate; do
        [[ -z "$candidate" || "$candidate" == "$target_name" ]] && continue
        if grep -qF -- "$candidate" "${dest}/${rel}" 2>/dev/null; then
          log_warn "$(t wf_stale_filename_warn "$rel" "$candidate")"
          found_any="true"
        fi
      done < <(_known_filenames_for_key "$key")
    done
  done

  [[ "$found_any" == "true" ]] && return 1
  return 0
}

# _verify_workflows_installed <chemin_relatif...>
# Vérifie que chaque fichier copié est bien présent dans le dossier de
# destination. Affiche une erreur claire et retourne 1 si l'un d'eux manque.
_verify_workflows_installed() {
  local dest="${INSTALL_DIR}/user/default/workflows"
  local rel missing=0

  for rel in "$@"; do
    if [[ ! -f "${dest}/${rel}" ]]; then
      log_error "$(t wf_missing_after_copy "$rel")"
      missing=1
    fi
  done

  if (( missing == 1 )); then
    log_error "$(t wf_not_installed_properly "$dest")"
    return 1
  fi

  return 0
}
