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
# H3_PRESET_WORKFLOWS / H3_PRESET_WORKFLOW_GITHUB_SOURCES (optionnel) /
# H3_PRESET_WORKFLOW_CIVITAI_URLS (optionnel) (config.env). Ajouter un
# preset ne nécessite aucune modification ici — voir le commentaire dans
# config.env.

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
      log_warn "$(t presets_unknown_ignored "$t" "${H3_PRESET_NAMES[*]}")"
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

# _preset_civitai_models_ref <nom> -> nom de variable du manifeste CivitAI du
# preset (PRESET_<NOM_MAJ>_CIVITAI_MODELS) sur stdout. Même convention que
# _preset_manifest_ref() ci-dessus, tableau distinct et optionnel : la
# plupart des presets n'ont aucun fichier servi directement par CivitAI (ils
# viennent tous de HuggingFace, cf. PRESET_<NOM> ci-dessus) — absent de
# config.env dans ce cas, ce n'est pas une erreur. Format d'une entrée :
# "url|chemin_relatif_a_models" (pas de sous-dossier séparé, contrairement à
# PRESET_<NOM> : le chemin complet, y compris diffusion_models/ etc., est
# porté par l'entrée elle-même — un fichier CivitAI n'a pas de "chemin dans
# le repo" à répliquer).
_preset_civitai_models_ref() {
  local name="$1"
  echo "PRESET_${name^^}_CIVITAI_MODELS"
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

# _dasiwa_variant_marker -> chemin sur stdout
# Fichier marqueur enregistrant QUELLE variante H3_DASIWA_CHECKPOINT_VARIANT
# a été réellement téléchargée avec succès la dernière fois, sur
# ${INSTALL_DIR}/models — donc sur /workspace, le volume PERSISTANT RunPod
# (survit à un redémarrage automatique du conteneur, contrairement au state
# file .minimax_installer_state qui vit sous PROJECT_ROOT/opt — voir
# lib/utils.sh state_file()). C'est ce marqueur, pas juste la variable
# d'environnement du run en cours, qui permet à _dasiwa_variant_guard()
# ci-dessous de savoir si CE run s'apprête à changer silencieusement ce qui
# est déjà sur disque.
_dasiwa_variant_marker() {
  echo "${INSTALL_DIR}/models/.dasiwa_variant_installed"
}

# _dasiwa_variant_guard <presets_csv>
# Garde d'autorisation : n'a d'effet que si le preset "dasiwa_mmh3v12" (seul
# consommateur de H3_DASIWA_CHECKPOINT_VARIANT) est actif. Si un marqueur
# existe déjà (une installation précédente a réussi avec une variante) ET
# que la variante résolue pour CE run est différente, le téléchargement
# n'est PAS lancé automatiquement : c'est exactement le scénario vécu — un
# redémarrage automatique de l'entrypoint (crash/OOM/coupure réseau pendant
# le gros téléchargement, PAS une action volontaire de l'utilisateur) qui
# retombe sur "pruned" par défaut et retélécharge ~42 Go en écrasant/
# doublant un choix "dasiwa_hybrid" déjà en place. On exige soit une
# confirmation interactive (confirm(), lib/utils.sh — répond automatiquement
# "non" en contexte non-interactif, ex. docker-entrypoint.sh sans tty : donc
# refuse par défaut, jamais de blocage en attente d'une réponse qui ne
# viendra jamais), soit un déblocage explicite et volontaire via
# H3_DASIWA_ALLOW_VARIANT_CHANGE=true (à positionner sciemment, jamais un
# comportement par défaut). En cas de refus, retourne 1 : download_preset_models()
# s'arrête AVANT tout téléchargement (aucun fichier touché), et
# install.sh (les deux points d'appel) traite déjà ce cas comme un échec
# non bloquant pour le reste de l'installation (log_warn
# install_preset_download_incomplete), donc ni exit ni perte du reste de
# l'install.
_dasiwa_variant_guard() {
  local presets_csv="$1"
  [[ ",${presets_csv}," == *",dasiwa_mmh3v12,"* ]] || return 0

  local marker; marker="$(_dasiwa_variant_marker)"
  [[ -f "$marker" ]] || return 0

  local installed; installed="$(<"$marker")"
  installed="${installed//[$'\t\r\n ']/}"
  [[ -z "$installed" || "$installed" == "$H3_DASIWA_CHECKPOINT_VARIANT" ]] && return 0

  log_error "$(t dasiwa_variant_mismatch_title "$installed" "$H3_DASIWA_CHECKPOINT_VARIANT")"
  log_error "$(t dasiwa_variant_mismatch_detail)"

  if [[ "${H3_DASIWA_ALLOW_VARIANT_CHANGE:-false}" == "true" ]]; then
    log_warn "$(t dasiwa_variant_mismatch_override)"
    return 0
  fi

  if confirm "$(t dasiwa_variant_mismatch_confirm "$installed" "$H3_DASIWA_CHECKPOINT_VARIANT")"; then
    return 0
  fi

  log_error "$(t dasiwa_variant_mismatch_aborted)"
  return 1
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

  _dasiwa_variant_guard "$presets_csv" || return 1

  local -a repos=()
  # shellcheck disable=SC2207
  repos=($(preset_required_repos "$presets_csv"))
  if ! hf_check_required_access "${repos[@]}"; then
    log_error "$(t presets_download_cancelled)"
    log_error "$(t presets_download_cancelled_fix)"
    return 1
  fi

  local base="${INSTALL_DIR}/models"
  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  # Compteur global "[i/N]" affiché par announce_download() (lib/utils.sh) —
  # somme des entrées de tous les presets actifs, indépendant du compteur
  # utilisé par download_missing_models() pour l'installation standard.
  local total=0 name ref entry civitai_ref
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    ref="$(_preset_manifest_ref "$name")"
    local -n manifest_count="$ref"
    total=$((total + ${#manifest_count[@]}))
    # + fichier(s) servis directement par CivitAI pour ce preset (variante
    # dasiwa_hybrid, etc.) — même compteur global "[i/N]" que le manifeste
    # HuggingFace ci-dessus, voir _preset_civitai_models_ref().
    civitai_ref="$(_preset_civitai_models_ref "$name")"
    if declare -p "$civitai_ref" &>/dev/null; then
      local -n civitai_count_arr="$civitai_ref"
      total=$((total + ${#civitai_count_arr[@]}))
    fi
  done
  export DOWNLOAD_FILE_TOTAL="$total"
  export DOWNLOAD_FILE_INDEX=0

  local repo path subdir dest_root dest_dir
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    log_step "$(t presets_downloading_models_step "$name")"
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

  # Fichier(s) CivitAI additionnels — après le manifeste HuggingFace, jamais
  # à la place : un preset peut mélanger les deux sources selon la variante
  # active (ex. dasiwa_mmh3v12/H3_DASIWA_CHECKPOINT_VARIANT, config.env).
  # download_civitai_model() (lib/models.sh, déjà sourcé avant lib/presets.sh
  # dans install.sh) gère elle-même reprise/retry/log — aucune logique de
  # téléchargement dupliquée ici.
  local civitai_entry civitai_url civitai_path
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    civitai_ref="$(_preset_civitai_models_ref "$name")"
    declare -p "$civitai_ref" &>/dev/null || continue
    local -n civitai_manifest="$civitai_ref"
    [[ ${#civitai_manifest[@]} -eq 0 ]] && continue
    for civitai_entry in "${civitai_manifest[@]}"; do
      IFS='|' read -r civitai_url civitai_path <<< "$civitai_entry"
      [[ -z "$civitai_url" || -z "$civitai_path" ]] && continue
      download_civitai_model "$civitai_url" "${base}/${civitai_path}" || return 1
    done
  done

  # Marqueur écrit UNIQUEMENT après succès complet de tout ce qui précède
  # (tout `|| return 1` ci-dessus saute cette ligne) : voir
  # _dasiwa_variant_guard()/_dasiwa_variant_marker() plus haut. N'enregistre
  # que si dasiwa_mmh3v12 est actif — les autres presets ne touchent pas à
  # H3_DASIWA_CHECKPOINT_VARIANT.
  if [[ ",${presets_csv}," == *",dasiwa_mmh3v12,"* ]]; then
    mkdir -p "$base"
    echo "$H3_DASIWA_CHECKPOINT_VARIANT" > "$(_dasiwa_variant_marker)"
  fi

  log_ok "$(t presets_models_downloaded "$presets_csv")"
}

# _preset_node_repos_ref <nom> -> nom de variable du tableau de nœuds custom
# du preset (PRESET_<NOM_MAJ>_NODE_REPOS) sur stdout. Même convention que
# _preset_manifest_ref() ci-dessus, tableau distinct : un preset peut n'avoir
# aucun nœud custom (le support H3 étant natif dans ce cas), ce tableau est
# alors absent de config.env.
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
    local -n node_repo_arr="$ref"
    [[ ${#node_repo_arr[@]} -eq 0 ]] && continue
    log_step "$(t presets_nodes_step "$name")"
    mkdir -p "${INSTALL_DIR}/custom_nodes"
    for repo_url in "${node_repo_arr[@]}"; do
      _clone_or_update_node_repo "$repo_url" "true"
    done
  done

  [[ "$any_declared" == "false" ]] && return 0
  log_ok "$(t presets_nodes_up_to_date "$presets_csv")"
  return 0
}

# _preset_pip_packages_ref <nom> -> nom de variable du tableau de paquets pip
# du preset (PRESET_<NOM_MAJ>_PIP_PACKAGES) sur stdout. Même convention que
# _preset_node_repos_ref() ci-dessus.
#
# Pourquoi ce mécanisme distinct de install_preset_nodes() (qui installe
# déjà automatiquement le requirements.txt d'un nœud custom cloné, si
# présent) : certains nœuds custom tiers déclarent leurs dépendances via
# pyproject.toml plutôt qu'un requirements.txt classique (ex.
# muse_director_seedhunt ci-dessous) — _clone_or_update_node_repo() ne lit
# jamais pyproject.toml (cf. son commentaire, lib/nodes.sh), donc ces
# dépendances ne seraient jamais installées sans ce tableau explicite. Scopé
# au preset (pas à OPTIONAL_NODE_REPOS/requirements.txt du projet) pour ne
# jamais imposer un paquet pip à une installation standard qui n'a pas
# activé ce preset précis.
_preset_pip_packages_ref() {
  local name="$1"
  echo "PRESET_${name^^}_PIP_PACKAGES"
}

# install_preset_pip_packages <presets_csv>
# Installe (dans le venv du projet) les paquets pip déclarés par les presets
# actifs, un par un via `pip install <paquet>` (paquets simples, versionnés
# ou non selon la déclaration dans config.env — jamais un fichier
# requirements.txt ici, voir _preset_pip_packages_ref ci-dessus pour le
# pourquoi). Silencieux si aucun preset actif ne déclare ce tableau (cas
# courant). Jamais bloquant : un échec logue un avertissement et passe au
# paquet suivant, même logique que install_preset_nodes().
install_preset_pip_packages() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local name ref pkg any_declared="false"
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    ref="$(_preset_pip_packages_ref "$name")"
    # Garde requise sous `set -u`, même logique que install_preset_nodes().
    declare -p "$ref" &>/dev/null || continue
    any_declared="true"
    local -n pkg_arr="$ref"
    [[ ${#pkg_arr[@]} -eq 0 ]] && continue
    log_step "$(t presets_pip_step "$name")"
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    for pkg in "${pkg_arr[@]}"; do
      if python -m pip install --quiet "$pkg" >>"$LOG_FILE" 2>&1; then
        log_ok "$(t presets_pip_installed "$pkg" "$name")"
      else
        log_warn "$(t presets_pip_install_failed "$pkg" "$name" "$LOG_FILE")"
      fi
    done
    deactivate
  done

  [[ "$any_declared" == "false" ]] && return 0
  log_ok "$(t presets_pip_up_to_date "$presets_csv")"
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
    log_step "$(t presets_symlinks_step "$name")"
    for entry in "${links[@]}"; do
      IFS='|' read -r target_rel link_rel <<< "$entry"
      target_abs="${base}/${target_rel}"
      link_abs="${base}/${link_rel}"
      if [[ ! -f "$target_abs" ]]; then
        log_warn "$(t presets_symlink_target_missing "$link_rel" "$target_rel")"
        continue
      fi
      mkdir -p "$(dirname "$link_abs")"
      if [[ -L "$link_abs" && "$(readlink -f "$link_abs")" == "$(readlink -f "$target_abs")" ]]; then
        continue
      fi
      ln -sf "$target_abs" "$link_abs"
      log_ok "$(t presets_symlink_created "$link_rel" "$target_rel")"
    done
  done

  if [[ "$any_declared" == "false" ]]; then
    return 0
  fi
  log_ok "$(t presets_symlinks_up_to_date "$presets_csv")"
}

# install_preset_workflows <presets_csv>
# Installe le(s) workflow(s) ComfyUI associé(s) à chaque preset actif dans
# ${INSTALL_DIR}/user/default/workflows — jamais via install_workflows()
# (lib/workflows.sh), dont la ré-écriture de noms de fichier par palier
# (_patch_workflow_tier_filenames) ne doit jamais s'appliquer aux noms de
# fichiers figés d'un preset.
#
# Trois sources possibles, dans cet ordre de priorité, chacune retombant sur
# la suivante en cas d'échec (réseau, dépôt/CivitAI indisponible, contenu
# invalide) — jamais d'échec bloquant pour ce seul détail :
#
# 1. H3_PRESET_WORKFLOW_GITHUB_SOURCES (config.env, optionnel) : TOUTES les
#    versions présentes dans le dossier d'un dépôt GitHub tiers sont
#    synchronisées (pas une seule) — pour un preset comme dasiwa_mmh3v12,
#    dont l'auteur (darksidewalker) publie régulièrement de nouvelles
#    versions numérotées dans son dépôt officiel
#    (github.com/darksidewalker/dasiwa-comfyui-workflows), ça évite de
#    figer arbitrairement UNE version côté installateur : l'utilisateur
#    retrouve tout l'historique disponible dans le menu workflows de ComfyUI
#    et choisit celle qu'il préfère, et une nouvelle version publiée en amont
#    apparaît automatiquement au prochain install/update sans modification
#    de ce projet.
# 2. H3_PRESET_WORKFLOW_CIVITAI_URLS (config.env, optionnel) : un fichier
#    unique téléchargé directement depuis CivitAI. À la différence de (1),
#    une URL CivitAI de ce type pointe une version de modèle CivitAI
#    précise et figée (pas de notion de "dernière version" côté CivitAI sur
#    cette route de téléchargement) — à réserver aux presets pour lesquels
#    aucun dépôt GitHub source n'est disponible.
# 3. Repli final : copie de TOUS les fichiers .json bundlés localement dans
#    presets/<nom>/ (et non plus un seul fichier nommé dans
#    H3_PRESET_WORKFLOWS) — pour qu'un preset multi-versions (comme
#    dasiwa_mmh3v12) garde le même choix de versions même hors ligne ou si
#    GitHub/CivitAI sont injoignables. Un preset qui n'a jamais eu qu'un seul
#    fichier bundlé (ex. muse_director_seedhunt) garde exactement l'ancien
#    comportement, le glob ne retombant que sur ce fichier unique.
install_preset_workflows() {
  local presets_csv="$1"
  [[ -z "$presets_csv" ]] && return 0

  local dest="${INSTALL_DIR}/user/default/workflows"
  mkdir -p "$dest"

  local -a names=()
  IFS=',' read -ra names <<< "$presets_csv"

  local name github_src owner_repo branch subfolder civitai_url rel target
  local preset_dir f synced
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue

    # 1) Dépôt GitHub tiers — toutes les versions disponibles.
    github_src="${H3_PRESET_WORKFLOW_GITHUB_SOURCES[$name]:-}"
    if [[ -n "$github_src" ]]; then
      IFS='|' read -r owner_repo branch subfolder <<< "$github_src"
      log_step "$(t presets_workflows_github_step "$name" "$owner_repo" "$subfolder" "$branch")"
      if _sync_preset_workflow_versions_from_github "$owner_repo" "$branch" "$subfolder" "$dest"; then
        continue
      fi
      log_warn "$(t presets_workflows_github_failed "$name")"
    fi

    # 2) Fichier unique CivitAI (repli, ou source principale si aucun dépôt
    #    GitHub n'est déclaré pour ce preset).
    rel="${H3_PRESET_WORKFLOWS[$name]:-}"
    civitai_url="${H3_PRESET_WORKFLOW_CIVITAI_URLS[$name]:-}"
    if [[ -n "$civitai_url" && -n "$rel" ]]; then
      target="${dest}/$(basename "$rel")"
      log_step "$(t presets_workflows_civitai_step "$name")"
      if _download_preset_workflow_from_civitai "$civitai_url" "$target"; then
        log_ok "$(t presets_workflows_civitai_downloaded "$name" "$(basename "$rel")")"
        continue
      fi
      log_warn "$(t presets_workflows_civitai_failed "$name")"
    fi

    # 3) Copie locale bundlée — tous les .json du dossier du preset.
    preset_dir="${PROJECT_ROOT}/presets/${name}"
    if [[ ! -d "$preset_dir" ]]; then
      log_warn "$(t presets_workflows_none_associated "$name")"
      continue
    fi
    synced=0
    for f in "${preset_dir}"/*.json; do
      [[ -f "$f" ]] || continue
      cp -f "$f" "${dest}/$(basename "$f")"
      synced=$((synced + 1))
    done
    if [[ "$synced" -gt 0 ]]; then
      log_ok "$(t presets_workflows_local_installed "$name" "$synced")"
    else
      log_warn "$(t presets_workflows_none_bundled "$name" "$preset_dir")"
    fi
  done
}

# _sync_preset_workflow_versions_from_github <owner/repo> <branch> <subfolder> <dest_dir>
# Télécharge l'archive tar.gz d'un dépôt GitHub public via codeload.github.com
# (pas besoin de git installé ni d'authentification pour un dépôt public),
# puis copie dans <dest_dir> TOUS les fichiers .json valides trouvés sous
# <subfolder>/ dans cette archive — une version amont = un fichier = une
# entrée dans le menu workflows de ComfyUI, à l'utilisateur de choisir.
# Chaque fichier est validé (_is_valid_workflow_json) avant copie ; un
# fichier individuellement invalide est ignoré (avertissement) sans faire
# échouer la synchronisation des autres. Retourne 0 si au moins un fichier a
# été synchronisé, 1 sinon (réseau, dépôt/branche/sous-dossier introuvable,
# archive vide ou corrompue) — toujours en repli non bloquant, voir
# install_preset_workflows() ci-dessus.
_sync_preset_workflow_versions_from_github() {
  local owner_repo="$1" branch="$2" subfolder="$3" dest_dir="$4"
  local tmp_dir; tmp_dir="$(mktemp -d)"
  local tar_file="${tmp_dir}/repo.tar.gz"
  local tarball_url="https://codeload.github.com/${owner_repo}/tar.gz/refs/heads/${branch}"

  if ! curl -sS -L --retry 3 --retry-delay 3 -o "$tar_file" "$tarball_url" 2>/dev/null || [[ ! -s "$tar_file" ]]; then
    log_warn "$(t presets_github_archive_failed "$owner_repo")"
    rm -rf -- "$tmp_dir"
    return 1
  fi

  if ! tar -xzf "$tar_file" -C "$tmp_dir" 2>/dev/null; then
    log_warn "$(t presets_github_archive_invalid "$owner_repo")"
    rm -rf -- "$tmp_dir"
    return 1
  fi

  # GitHub nomme le dossier extrait "<repo>-<branche>/" (le owner n'y figure
  # pas). On cherche plutôt que de reconstruire le nom, plus robuste face à
  # d'éventuelles variations (branche avec des "/", etc.).
  local extracted_dir
  extracted_dir="$(find "$tmp_dir" -mindepth 2 -maxdepth 2 -type d -path "*/${subfolder}" -print -quit)"

  if [[ -z "$extracted_dir" || ! -d "$extracted_dir" ]]; then
    log_warn "$(t presets_github_subfolder_missing "$owner_repo" "$subfolder" "$branch")"
    rm -rf -- "$tmp_dir"
    return 1
  fi

  mkdir -p "$dest_dir"
  local synced=0 f base
  for f in "${extracted_dir}"/*.json; do
    [[ -f "$f" ]] || continue
    base="$(basename "$f")"
    if _is_valid_workflow_json "$f"; then
      cp -f "$f" "${dest_dir}/${base}"
      synced=$((synced + 1))
    else
      log_warn "$(t presets_github_json_skipped "$owner_repo" "$base")"
    fi
  done

  rm -rf -- "$tmp_dir"

  if [[ "$synced" -eq 0 ]]; then
    log_warn "$(t presets_github_none_synced "$owner_repo" "$subfolder")"
    return 1
  fi

  log_ok "$(t presets_github_synced "$owner_repo" "$subfolder" "$synced")"
  return 0
}

# _download_preset_workflow_from_civitai <url> <dest_file>
# Télécharge un workflow ComfyUI (fichier .json) depuis une URL de
# téléchargement direct CivitAI ("civitai.com/api/download/models/..." ou
# "civitai.red/..."), avec la même authentification optionnelle que
# install_lora.sh (CIVITAI_API_KEY, en-tête "Authorization: Bearer ..." —
# jamais requise pour un fichier public) et une vérification de contenu :
# écriture dans un fichier temporaire, validé (JSON syntaxiquement correct,
# jamais une page d'erreur HTML déguisée) avant de ne remplacer <dest_file>
# qu'à ce moment-là — pour ne jamais laisser un workflow fonctionnel
# préexistant dans un état cassé suite à un téléchargement raté ou une
# réponse invalide (connexion CivitAI requise, 404, erreur Cloudflare...).
# Retourne 0 en cas de succès, 1 sinon. Usage exclusivement en repli non
# bloquant, voir install_preset_workflows() ci-dessus.
_download_preset_workflow_from_civitai() {
  local url="$1" dest_file="$2"
  local tmp_file; tmp_file="$(mktemp "${dest_file}.XXXXXX")"

  local -a auth_args=()
  [[ -n "${CIVITAI_API_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer ${CIVITAI_API_KEY}")

  local http_code
  http_code="$(curl -sS -L --retry 3 --retry-delay 3 "${auth_args[@]}" -o "$tmp_file" -w '%{http_code}' "$url" 2>/dev/null || true)"

  if [[ ! "$http_code" =~ ^2 ]] || [[ ! -s "$tmp_file" ]]; then
    log_warn "$(t presets_civitai_download_failed "${http_code:-$(t gpu_cuda_unknown)}")"
    rm -f -- "$tmp_file" 2>/dev/null || true
    return 1
  fi

  if ! _is_valid_workflow_json "$tmp_file"; then
    log_warn "$(t presets_civitai_invalid_content)"
    rm -f -- "$tmp_file" 2>/dev/null || true
    return 1
  fi

  mv -f -- "$tmp_file" "$dest_file"
}

# _is_valid_workflow_json <fichier>
# Vérifie que <fichier> est un JSON syntaxiquement valide (python3, toujours
# disponible sur ce projet — voir lib/python.sh) plutôt qu'une page d'erreur
# HTML déguisée. Contrôle volontairement minimal : aucune vérification de la
# structure interne du workflow (nœuds attendus, etc.), CivitAI n'exposant
# aucune garantie de schéma au-delà de "c'est un fichier JSON".
_is_valid_workflow_json() {
  local file="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$file" >/dev/null 2>&1
    return $?
  fi
  # Repli sans python3 (ne devrait pas arriver sur ce projet) : au moins
  # écarter une page HTML évidente.
  ! head -c 512 -- "$file" 2>/dev/null | grep -qi '<!doctype html\|<html[ >]'
}
