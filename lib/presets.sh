#!/usr/bin/env bash
# lib/presets.sh — presets : jeux de modèles additionnels dédiés à un
# workflow particulier, activables sans toucher au comportement standard du
# projet (paliers H3_TIER, workflows H3_WORKFLOWS).
#
# Un preset ne redéfinit RIEN de l'installation standard : il ajoute des
# fichiers en plus (téléchargés via download_hf_file(), lib/download.sh —
# même logique d'idempotence/reprise/vérification que le reste du projet),
# éventuellement des nœuds custom dédiés (via _clone_or_update_node_repo(),
# lib/nodes.sh — même logique de clonage/mise à jour que les nœuds optionnels
# globaux), et installe un workflow ComfyUI dédié. H3_PRESETS vide (défaut)
# => ce fichier ne fait rien.
#
# Source de vérité pour la liste des presets connus et leur manifeste :
# H3_PRESET_NAMES / PRESET_<NOM> / PRESET_<NOM>_NODE_REPOS (optionnel) /
# H3_PRESET_WORKFLOWS (config.env). Ajouter un preset ne nécessite aucune
# modification ici — voir le commentaire dans config.env.

# resolve_h3_presets — normalise H3_PRESETS/--preset= : liste séparée par des
# virgules, jetons vides ignorés silencieusement, jetons inconnus ignorés
# avec avertissement (jamais d'erreur bloquante — un preset est toujours
# additif, jamais requis). Chaîne vide en entrée -> chaîne vide en sortie
# (aucun preset actif, comportement historique).
resolve_h3_presets() {
  local raw="${H3_PRESETS:-}"
  [[ -z "${raw//,/}" ]] && { echo ""; return 0; }

  local -a tokens=() valid=()
  IFS=',' read -ra tokens <<< "$raw"
  local t known name
  for t in "${tokens[@]}"; do
    [[ -z "$t" ]] && continue
    known="false"
    for name in "${H3_PRESET_NAMES[@]}"; do
      [[ "$t" == "$name" ]] && { known="true"; break; }
    done
    if [[ "$known" == "true" ]]; then
      valid+=("$t")
    else
      log_warn "H3_PRESETS/--preset= : preset inconnu ignoré : '${t}' (disponibles : ${H3_PRESET_NAMES[*]})."
    fi
  done

  local IFS=','
  echo "${valid[*]}"
}

# _preset_manifest_ref <nom> -> nom de variable du manifeste (PRESET_<NOM_MAJ>)
# sur stdout. N'existe que pour centraliser la convention de nommage
# (nameref requis par le caller pour itérer le tableau).
_preset_manifest_ref() {
  local name="$1"
  echo "PRESET_${name^^}"
}

# preset_required_repos <presets_csv> -> liste dédoublonnée (une par ligne,
# via echo "${arr[*]}") des dépôts HuggingFace référencés par les presets
# actifs — même contrat que h3_required_repos() (lib/models.sh), pour
# réutiliser hf_check_required_access() (lib/huggingface.sh) telle quelle.
preset_required_repos() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local -A seen=()
  local -a repos=()
  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local name ref entry repo path subdir
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    ref="$(_preset_manifest_ref "$name")"
    local -n manifest="$ref"
    for entry in "${manifest[@]}"; do
      IFS='|' read -r repo path subdir <<< "$entry"
      [[ -n "$repo" && -z "${seen[$repo]:-}" ]] && { seen[$repo]=1; repos+=("$repo"); }
    done
  done

  echo "${repos[*]}"
}

# download_preset_models <presets_csv>
# Télécharge chaque fichier manquant/invalide des manifestes des presets
# actifs. Réutilise download_hf_file() (lib/download.sh) telle quelle :
# aucune logique de reprise/vérification/retry dupliquée ici. Vérifie
# l'accès aux dépôts requis (licence gated) une seule fois pour tous les
# presets avant tout téléchargement, comme download_missing_models()
# (lib/models.sh) le fait pour l'installation standard.
download_preset_models() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local -a repos=()
  # shellcheck disable=SC2207
  repos=($(preset_required_repos "$presets_csv"))
  if ! hf_check_required_access "${repos[@]}"; then
    log_error "Téléchargement des modèles de preset annulé : accès à au moins un dépôt non confirmé."
    log_error "Acceptez la licence sur le(s) dépôt(s) concerné(s) puis relancez avec le(s) même(s) --preset=."
    return 1
  fi

  local base="${INSTALL_DIR}/models"
  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  # Compteur global "[i/N]" affiché par announce_download() (lib/utils.sh) —
  # somme des entrées de tous les presets actifs, indépendant du compteur
  # utilisé par download_missing_models() pour l'installation standard.
  local total=0 name ref entry
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    ref="$(_preset_manifest_ref "$name")"
    local -n manifest_count="$ref"
    total=$((total + ${#manifest_count[@]}))
  done
  export DOWNLOAD_FILE_TOTAL="$total"
  export DOWNLOAD_FILE_INDEX=0

  local repo path subdir dest_root dest_dir
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    log_step "Preset '${name}' — téléchargement des modèles associés"
    ref="$(_preset_manifest_ref "$name")"
    local -n manifest="$ref"
    for entry in "${manifest[@]}"; do
      IFS='|' read -r repo path subdir <<< "$entry"
      dest_root="$base"
      [[ -n "$subdir" ]] && dest_root="${base}/${subdir}"
      # download_hf_file() ne crée que $dest_root lui-même (mkdir -p
      # "$dest_dir" en son sein) : quand $path contient encore un
      # sous-dossier (ex. "loras/xxx.safetensors" avec subdir vide), le
      # sous-dossier final doit être créé ici au préalable — même convention
      # que download_missing_models() (lib/models.sh) pour text_encoder/vae.
      dest_dir="$(dirname "${dest_root}/${path}")"
      mkdir -p "$dest_dir"
      download_hf_file "$repo" "$path" "$dest_root" || return 1
    done
  done

  log_ok "Modèles du/des preset(s) '${presets_csv}' téléchargés."
}

# _preset_node_repos_ref <nom> -> nom de variable du tableau de nœuds custom
# du preset (PRESET_<NOM_MAJ>_NODE_REPOS) sur stdout. Même convention que
# _preset_manifest_ref() ci-dessus, tableau distinct : un preset peut n'avoir
# aucun nœud custom (cas le plus courant — ex. aistudynow n'en a pas besoin,
# le support H3 étant natif), ce tableau est alors absent de config.env.
_preset_node_repos_ref() {
  local name="$1"
  echo "PRESET_${name^^}_NODE_REPOS"
}

# install_preset_nodes <presets_csv>
# Clone/met à jour les nœuds custom déclarés par les presets actifs, un par
# un, réutilisant _clone_or_update_node_repo() (lib/nodes.sh) telle quelle —
# aucune logique de clonage dupliquée ici. Silencieux si un preset actif ne
# déclare aucun tableau PRESET_<NOM>_NODE_REPOS (cas courant) : ce n'est pas
# une erreur, juste l'absence de nœud dédié pour ce preset. Jamais bloquant,
# comme install_optional_nodes() : un échec de clonage logue un avertissement
# et n'interrompt ni les autres nœuds ni le reste de l'installation.
install_preset_nodes() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local name ref repo_url any_declared="false"
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    ref="$(_preset_node_repos_ref "$name")"
    # Garde requise sous `set -u` : un nameref vers une variable jamais
    # déclarée (preset sans nœud custom, cas courant) lèverait une erreur non
    # bloquante voulue autrement — declare -p vérifie l'existence sans y
    # toucher.
    declare -p "$ref" &>/dev/null || continue
    any_declared="true"
    local -n repos="$ref"
    [[ ${#repos[@]} -eq 0 ]] && continue
    log_step "Preset '${name}' — nœuds custom associés"
    mkdir -p "${INSTALL_DIR}/custom_nodes"
    for repo_url in "${repos[@]}"; do
      _clone_or_update_node_repo "$repo_url" "true"
    done
  done

  [[ "$any_declared" == "false" ]] && return 0
  log_ok "Nœuds custom du/des preset(s) '${presets_csv}' à jour."
  return 0
}

# _preset_symlinks_ref <nom> -> nom de variable du tableau de liens
# symboliques du preset (PRESET_<NOM_MAJ>_SYMLINKS) sur stdout. Même
# convention que _preset_node_repos_ref() ci-dessus : absent de config.env
# pour la plupart des presets (cas courant), ce n'est pas une erreur.
_preset_symlinks_ref() {
  local name="$1"
  echo "PRESET_${name^^}_SYMLINKS"
}

# preset_replaces_standard_tier <presets_csv>
# "true" sur stdout si au moins un preset actif figure dans
# H3_PRESET_REPLACES_STANDARD_TIER (config.env) — signifie qu'il fournit
# lui-même un jeu complet fl2va/ref2va/text encoder et que télécharger EN
# PLUS le palier H3_TIER standard ne ferait que dupliquer des poids pour le
# même rôle. "false" sinon (cas par défaut : les presets sont additifs).
# Utilisé par install.sh pour décider de sauter download_h3_models().
preset_replaces_standard_tier() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && { echo "false"; return 0; }

  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local name replaces_name
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    for replaces_name in "${H3_PRESET_REPLACES_STANDARD_TIER[@]}"; do
      if [[ "$name" == "$replaces_name" ]]; then
        echo "true"
        return 0
      fi
    done
  done
  echo "false"
}

# install_preset_symlinks <presets_csv>
# Certains workflows (ex. les nœuds MiniMax H3 du pack DaSiWa-Nodes)
# attendent leurs fichiers modèles sous un sous-dossier précis (ex.
# "diffusion_models/MiniMaxH3/…") alors que le vrai chemin du dépôt Hugging
# Face — donc l'emplacement où download_hf_file() les place réellement — est
# différent (ex. "diffusion_models/…", sans "MiniMaxH3"). Comme le manifeste
# PRESET_<NOM> (repo|chemin_repo|sous-dossier) sert à la fois à l'URL de
# téléchargement ET à la destination locale, il ne peut pas exprimer "range
# ce fichier sous un autre nom/chemin local" sans casser le téléchargement —
# d'où ce tableau séparé, appliqué UNIQUEMENT après download_preset_models(),
# jamais couplé au manifeste lui-même.
# Format d'une entrée : "chemin_reel_relatif_a_models|chemin_lien_relatif_a_models"
# (le fichier cible doit déjà exister — silencieux, non bloquant sinon : log_warn
# et on continue, plutôt que d'interrompre l'installation pour un lien de confort).
install_preset_symlinks() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local base="${INSTALL_DIR}/models"
  local name ref entry target_rel link_rel target_abs link_abs any_declared="false"
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    ref="$(_preset_symlinks_ref "$name")"
    # Garde requise sous `set -u`, même logique que install_preset_nodes().
    declare -p "$ref" &>/dev/null || continue
    any_declared="true"
    local -n links="$ref"
    [[ ${#links[@]} -eq 0 ]] && continue
    log_step "Preset '${name}' — liens symboliques modèles"
    for entry in "${links[@]}"; do
      IFS='|' read -r target_rel link_rel <<< "$entry"
      target_abs="${base}/${target_rel}"
      link_abs="${base}/${link_rel}"
      if [[ ! -f "$target_abs" ]]; then
        log_warn "Lien symbolique '${link_rel}' ignoré — fichier cible introuvable (${target_rel}), a-t-il bien été téléchargé ?"
        continue
      fi
      mkdir -p "$(dirname "$link_abs")"
      if [[ -L "$link_abs" && "$(readlink -f "$link_abs")" == "$(readlink -f "$target_abs")" ]]; then
        continue
      fi
      ln -sf "$target_abs" "$link_abs"
      log_ok "Lien créé : ${link_rel} -> ${target_rel}"
    done
  done

  if [[ "$any_declared" == "false" ]]; then
    return 0
  fi
  log_ok "Lien(s) symbolique(s) du/des preset(s) '${presets_csv}' à jour."
}

# install_preset_workflows <presets_csv>
# Copie le workflow ComfyUI associé à chaque preset actif (H3_PRESET_WORKFLOWS,
# config.env) dans ${INSTALL_DIR}/user/default/workflows — jamais via
# install_workflows() (lib/workflows.sh), dont la ré-écriture de noms de
# fichier par palier (_patch_workflow_tier_filenames) ne doit jamais
# s'appliquer aux noms de fichiers figés d'un preset.
install_preset_workflows() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local dest="${INSTALL_DIR}/user/default/workflows"
  mkdir -p "$dest"

  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local name rel src target
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    rel="${H3_PRESET_WORKFLOWS[$name]:-}"
    if [[ -z "$rel" ]]; then
      log_warn "Preset '${name}' : aucun workflow associé (H3_PRESET_WORKFLOWS) — modèles installés, mais pas de workflow prêt à l'emploi."
      continue
    fi
    src="${PROJECT_ROOT}/${rel}"
    if [[ ! -f "$src" ]]; then
      log_warn "Preset '${name}' : workflow introuvable (${src}) — modèles installés, mais workflow non copié."
      continue
    fi
    target="${dest}/$(basename "$rel")"
    cp -f "$src" "$target"
    if [[ -f "$target" ]]; then
      log_ok "Workflow du preset '${name}' installé : $(basename "$rel")."
    else
      log_warn "Échec de copie du workflow du preset '${name}' (${src} -> ${target})."
    fi
  done
}
