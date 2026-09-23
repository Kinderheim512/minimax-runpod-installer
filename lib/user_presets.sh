#!/usr/bin/env bash
# lib/user_presets.sh — presets ComfyUI définis par l'utilisateur (modèles +
# nœuds custom + workflows), pilotés par un document JSON envoyé par le
# launcher (H3_USER_PRESETS_JSON) ou persisté sur le volume persistant
# (/workspace/.openfox/presets.json) pour survivre à un resync/recréation du
# pod.
#
# Ce fichier NE réimplémente AUCUNE logique d'installation : il traduit le
# JSON en appels aux primitives génériques déjà en place —
#   - nœuds custom  : _clone_or_update_node_repo() (lib/nodes.sh) — clone/
#                     mise à jour idempotent, détection auto de requirements.txt,
#                     épinglage optionnel "#<ref>", + un champ post_install
#                     exécuté UNE fois (marqueur + log, non bloquant) ;
#   - modèles       : download_hf_file() (lib/download.sh) pour les URLs
#                     Hugging Face "resolve", download_civitai_model()
#                     (lib/models.sh) pour tout autre lien direct (CivitAI,
#                     lien direct) — avec lien symbolique vers le chemin
#                     cible exact quand il diffère du chemin réel du dépôt ;
#   - workflows     : _sync_preset_workflow_versions_from_github() et
#                     _download_preset_workflow_from_civitai() (lib/presets.sh).
#
# Format du document JSON (version 1) :
#   {"version":1,"presets":{"nom":{"models":[{"url":"...","target":"..."}],
#   "nodes":[{"url":"...","post_install":"..."}],
#   "workflows":[{"github":"owner/repo|branche|sous-dossier"},{"url":"..."}]}}}
#
# No-op silencieux si aucun document n'est fourni ni persisté.

# Fichier de persistance : /workspace/.openfox/presets.json (volume persistant
# RunPod — même principe que H3_USER_CHOICES_FILE). Surchargable via
# H3_USER_PRESETS_FILE. /workspace est le point de montage persistant standard
# de RunPod, quel que soit INSTALL_DIR (qui vaut /opt/ComfyUI sur l'image
# Docker pré-installée, baked et non persistant).
# Persistence file: /workspace/.openfox/presets.json when RunPod's persistent
# mount exists (the standard mount, whatever INSTALL_DIR is), otherwise a path
# under INSTALL_DIR — /workspace only exists when a network volume is attached,
# and a missing mount must never make the write (or the install) fail.
# See docs/comfy-image-analysis.md §9 #8. Overridable via H3_USER_PRESETS_FILE.
_user_presets_default_file() {
  if [[ -d /workspace ]]; then
    printf '%s' "/workspace/.openfox/presets.json"
  else
    printf '%s' "${INSTALL_DIR}/user/.openfox/presets.json"
  fi
}
H3_USER_PRESETS_FILE="${H3_USER_PRESETS_FILE:-$(_user_presets_default_file)}"

user_presets_file() { echo "$H3_USER_PRESETS_FILE"; }

# persist_user_presets_json
# Écrit le document reçu via H3_USER_PRESETS_JSON vers le fichier persistant
# (/workspace/.openfox/presets.json). C'est ce qui garantit qu'un preset créé
# dans le launcher survit à un resync/recréation du pod : le launcher le
# renvoie comme variable d'environnement à chaque (re)création, et le pod le
# re-persiste ici — un patch manuel antérieur avait été perdu au dernier
# resync faute de cette persistance côté pod.
persist_user_presets_json() {
  [[ -n "${H3_USER_PRESETS_JSON:-}" ]] || return 0
  local f; f="$(user_presets_file)"
  # Never fatal: persistence is an optimisation (the launcher re-sends the
  # document on every pod start), so a read-only or missing mount must not
  # abort the install.
  if ! { mkdir -p "$(dirname "$f")" && printf '%s' "$H3_USER_PRESETS_JSON" > "$f"; } 2>/dev/null; then
    log_warn "could not persist user presets to ${f} — continuing (the launcher re-sends them on every start)."
    return 0
  fi
  log_info "$(t user_presets_persisted "$f")"
}

# load_user_presets_json
# Variable d'environnement d'abord (autorité du run en cours), fichier
# persistant ensuite (survit à un redémarrage automatique du conteneur).
load_user_presets_json() {
  if [[ -n "${H3_USER_PRESETS_JSON:-}" ]]; then
    printf '%s' "$H3_USER_PRESETS_JSON"
    return 0
  fi
  local f; f="$(user_presets_file)"
  # An absent file is the NORMAL case (no user presets, no persisted copy):
  # return empty SUCCESS. Returning 1 here made `json="$(load_user_presets_json)"`
  # abort install.sh under `set -e` — observed live on a pod with no network
  # volume, see docs/comfy-image-analysis.md §9 #8.
  if [[ -f "$f" ]]; then
    cat "$f"
  else
    printf ''
  fi
  return 0
}

# _user_presets_stream <json>
# Convertit le document JSON en un flux TSV (un enregistrement par ligne,
# champs séparés par tabulation) via python3 (toujours présent sur ce projet).
# Lignes :
#   MODEL\t<nom>\t<url>\t<target>
#   NODE\t<nom>\t<url>\t<post_install>
#   WF\t<nom>\t<github|url>\t<valeur>
# Chaque champ est assaini (tabulations/sauts de ligne remplacés par des
# espaces). Retourne non nul (rien sur stdout) si le JSON est invalide.
_user_presets_stream() {
  local json="$1"
  python3 - "$json" <<'PY'
import json, sys
try:
    doc = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
if not isinstance(doc, dict) or not isinstance(doc.get("presets"), dict):
    sys.exit(1)

def clean(v):
    return str(v or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")

for name, body in doc["presets"].items():
    if not isinstance(body, dict):
        continue
    for m in body.get("models") or []:
        if isinstance(m, dict) and m.get("url"):
            print("\t".join(clean(c) for c in ("MODEL", name, m.get("url"), m.get("target"))))
    for n in body.get("nodes") or []:
        if isinstance(n, dict) and n.get("url"):
            print("\t".join(clean(c) for c in ("NODE", name, n.get("url"), n.get("post_install"))))
    for w in body.get("workflows") or []:
        if not isinstance(w, dict):
            continue
        if w.get("github"):
            print("\t".join(clean(c) for c in ("WF", name, "github", w.get("github"))))
        elif w.get("url"):
            print("\t".join(clean(c) for c in ("WF", name, "url", w.get("url"))))
PY
}

# run_user_node_post_install <repo_url> <commande>
# Exécute le post_install d'un nœud custom UNE seule fois (marqueur
# .post_install_<nom>.done dans custom_nodes/, à CÔTÉ du dossier du nœud —
# jamais DANS le dépôt git, sinon il polluerait `git status --porcelain` et
# ferait sauter la mise à jour du nœud par _clone_or_update_node_repo), dans
# le dossier du nœud, avec journalisation. Non bloquant : un échec logue un
# avertissement et retourne toujours 0.
run_user_node_post_install() {
  local repo_url="$1" command="$2"
  [[ -n "$command" ]] || return 0
  # Suffixe "#<ref>" éventuel (épinglage, même convention que
  # _clone_or_update_node_repo) retiré avant de dériver le nom du dossier.
  local clean_url="${repo_url%%#*}"
  local name; name="$(basename "$clean_url" .git)"
  local target="${INSTALL_DIR}/custom_nodes/${name}"
  [[ -d "$target" ]] || return 0
  local marker="${INSTALL_DIR}/custom_nodes/.post_install_${name}.done"
  [[ -f "$marker" ]] && return 0
  log_info "$(t user_preset_post_install "$name")"
  if ( cd "$target" && bash -lc "$command" ) >>"$LOG_FILE" 2>&1; then
    touch "$marker"
    log_ok "$(t user_preset_post_install_done "$name")"
  else
    log_warn "$(t user_preset_post_install_failed "$name" "$LOG_FILE")"
  fi
  return 0
}

# install_user_preset_nodes <stream>
# Clone/met à jour les nœuds custom des presets utilisateur via
# _clone_or_update_node_repo() (même fonction que OPTIONAL_NODE_REPOS), puis
# exécute le post_install éventuel. Jamais bloquant.
install_user_preset_nodes() {
  local stream="$1"
  local kind name url post_install count=0
  # Lecture sur le descripteur 3 (pas stdin) : _clone_or_update_node_repo /
  # run_user_node_post_install lancent des commandes (git, bash -lc) qui
  # peuvent lire stdin et « avaler » la suite du flux — la boucle s'arrêtait
  # alors après la première entrée (bug vécu : un seul nœud/modèle installé).
  while IFS=$'\t' read -r kind name url post_install <&3; do
    [[ "$kind" == "NODE" ]] || continue
    _clone_or_update_node_repo "$url" "true"
    run_user_node_post_install "$url" "$post_install"
    count=$((count + 1))
  done 3<<< "$stream"
  [[ "$count" -gt 0 ]] && log_ok "$(t user_presets_nodes_done "$count")"
  return 0
}

# _download_user_preset_model <url> <target>
# Télécharge UN modèle d'un preset utilisateur vers models/<target> :
#   - URL Hugging Face "resolve" -> download_hf_file() (Xet, idempotent,
#     reprise), puis lien symbolique si le chemin cible diffère du chemin
#     réel du dépôt (même principe que install_preset_symlinks) ;
#   - tout autre lien (CivitAI, lien direct) -> download_civitai_model()
#     (curl générique, reprise -C -, auth optionnelle) vers le chemin exact.
_download_user_preset_model() {
  local url="$1" target="$2"
  [[ -n "$url" ]] || return 0
  local dest="${INSTALL_DIR}/models/${target}"

  if [[ "$url" == *"huggingface.co"* && "$url" == *"/resolve/"* ]]; then
    local repo path
    repo="$(_hf_url_repo "$url")"
    # Le suffixe "?download=true" (courant sur les liens HF copiés depuis le
    # navigateur) ne fait pas partie du chemin dans le dépôt : le retirer,
    # sinon `hf download` cherche un fichier littéralement nommé
    # "xxx.safetensors?download=true" et échoue.
    path="${url#*/resolve/}"; path="${path#*/}"; path="${path%%\?*}"
    if [[ -z "$repo" || -z "$path" ]]; then
      download_civitai_model "$url" "$dest"
      return $?
    fi
    mkdir -p "${INSTALL_DIR}/models/$(dirname "$path")"
    download_hf_file "$repo" "$path" "${INSTALL_DIR}/models" || return 1
    local src="${INSTALL_DIR}/models/${path}"
    if [[ "$(readlink -f "$src" 2>/dev/null)" != "$(readlink -f "$dest" 2>/dev/null)" ]]; then
      mkdir -p "$(dirname "$dest")"
      ln -sf "$src" "$dest"
      log_ok "$(t user_presets_model_linked "$(basename "$dest")")"
    fi
    return 0
  fi

  download_civitai_model "$url" "$dest"
}

# download_user_preset_models <stream>
# Télécharge chaque modèle des presets utilisateur, avec le compteur global
# "[i/N]" (DOWNLOAD_FILE_TOTAL) comme download_preset_models().
download_user_preset_models() {
  local stream="$1"
  local kind name url target total=0
  # Descripteur 3 (pas stdin) : les téléchargements lisent stdin et
  # consommeraient le reste du flux (cf. install_user_preset_nodes).
  while IFS=$'\t' read -r kind name url target <&3; do
    [[ "$kind" == "MODEL" ]] && total=$((total + 1))
  done 3<<< "$stream"
  export DOWNLOAD_FILE_TOTAL="$total"
  export DOWNLOAD_FILE_INDEX=0

  while IFS=$'\t' read -r kind name url target <&3; do
    [[ "$kind" == "MODEL" ]] || continue
    _download_user_preset_model "$url" "$target" \
      || log_warn "$(t user_presets_model_failed "$url")"
  done 3<<< "$stream"
  [[ "$total" -gt 0 ]] && log_ok "$(t user_presets_models_done "$total")"
  return 0
}

# _user_preset_github_parts <valeur>
# Normalise la source GitHub d'un workflow utilisateur en
# "owner/repo|branche|sous-dossier". Accepte :
#   - "owner/repo|branche|sous-dossier" (format documenté) ;
#   - "https://github.com/owner/repo/tree/branche/sous-dossier" (URL de page) ;
#   - "https://github.com/owner/repo" (branche par défaut = main).
# Une URL complète était prise telle quelle comme "owner/repo" par l'ancien
# code, ce qui produisait une archive invalide (URL codeload absurde) — d'où
# le "invalid or corrupted archive" et un workflow jamais installé.
_user_preset_github_parts() {
  local value="$1"
  if [[ "$value" == http*://*github.com/* ]]; then
    local rest="${value#*github.com/}"
    rest="${rest%%\?*}"; rest="${rest%%#*}"
    local owner repo branch subfolder
    owner="${rest%%/*}"; rest="${rest#*/}"
    repo="${rest%%/*}"; rest="${rest#*/}"
    repo="${repo%.git}"
    branch="main"; subfolder=""
    if [[ "$rest" == tree/* ]]; then
      rest="${rest#tree/}"
      branch="${rest%%/*}"
      [[ "$rest" == */* ]] && subfolder="${rest#*/}"
    elif [[ "$rest" == blob/* ]]; then
      rest="${rest#blob/}"; rest="${rest#*/}"
      subfolder="$(dirname "$rest")"
      [[ "$subfolder" == "." ]] && subfolder=""
    fi
    [[ -n "$owner" && -n "$repo" ]] || return 1
    [[ -n "$branch" ]] || branch="main"
    printf '%s/%s|%s|%s' "$owner" "$repo" "$branch" "$subfolder"
    return 0
  fi
  printf '%s' "$value"
}

# install_user_preset_workflows <stream>
# Installe les workflows des presets utilisateur dans
# ${INSTALL_DIR}/user/default/workflows :
#   - "github" -> _sync_preset_workflow_versions_from_github() (toutes les
#     versions du dossier d'un dépôt, même moteur que dasiwa_mmh3v12) ;
#   - "url"    -> _download_preset_workflow_from_civitai() (fichier JSON
#     unique, validé avant remplacement).
install_user_preset_workflows() {
  local stream="$1"
  local dest="${INSTALL_DIR}/user/default/workflows"
  mkdir -p "$dest"
  local kind name wfkind value count=0
  # Descripteur 3 (pas stdin) — cf. install_user_preset_nodes.
  while IFS=$'\t' read -r kind name wfkind value <&3; do
    [[ "$kind" == "WF" ]] || continue
    if [[ "$wfkind" == "github" ]]; then
      local parts owner_repo branch subfolder
      parts="$(_user_preset_github_parts "$value")" || {
        log_warn "$(t user_presets_workflow_source_invalid "$value")"
        continue
      }
      IFS='|' read -r owner_repo branch subfolder <<< "$parts"
      if _sync_preset_workflow_versions_from_github "$owner_repo" "$branch" "$subfolder" "$dest"; then
        count=$((count + 1))
      fi
    else
      local fname
      fname="$(basename "${value%%\?*}")"
      [[ "$fname" == *.json ]] || fname="${fname}.json"
      if _download_preset_workflow_from_civitai "$value" "${dest}/${fname}"; then
        count=$((count + 1))
      fi
    fi
  done 3<<< "$stream"
  [[ "$count" -gt 0 ]] && log_ok "$(t user_presets_workflows_done "$count")"
  return 0
}

# install_user_presets
# Point d'entrée appelé par install.sh (les deux branches, après les presets
# figés en dur) : persiste le document, puis installe nœuds, modèles et
# workflows des presets utilisateur. No-op si aucun document.
install_user_presets() {
  persist_user_presets_json

  local json
  json="$(load_user_presets_json)"
  [[ -n "$json" ]] || return 0

  log_step "$(t user_presets_step)"

  local stream
  if ! stream="$(_user_presets_stream "$json")"; then
    log_warn "$(t user_presets_json_invalid)"
    return 0
  fi
  [[ -n "$stream" ]] || return 0

  install_user_preset_nodes "$stream"
  download_user_preset_models "$stream"
  install_user_preset_workflows "$stream"
  log_ok "$(t user_presets_done)"
  return 0
}
