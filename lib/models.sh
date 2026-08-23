#!/usr/bin/env bash
# lib/models.sh — arborescence de modèles ComfyUI + téléchargement des poids
# MiniMax H3 officiels (dépôt Comfy-Org/MiniMax-H3, repackagé pour ComfyUI).
#
# Sources (vérifiées) :
#   https://huggingface.co/Comfy-Org/MiniMax-H3
#   https://docs.comfy.org/tutorials/video/minimax/minimax-h3
#   https://blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui
#
# Tailles indiquées à titre informatif pour l'estimation d'espace disque
# (relevées sur le dépôt HF au moment de l'écriture — vérifiées dynamiquement
# au téléchargement via la taille distante réelle, voir lib/download.sh).

create_model_folders() {
  log_step "$(t models_folders_step)"
  local base="${INSTALL_DIR}/models"
  for d in checkpoints diffusion_models text_encoders vae clip controlnet loras upscale_models; do
    mkdir -p "${base}/${d}"
  done
  log_ok "$(t models_folders_ready)"
}

# --- Manifeste des fichiers H3 -----------------------------------------------
# format : "repo|sous_chemin_dans_le_repo|palier|go_approx"
#
# Champ "repo" ajouté (architecture multi-dépôts) : chaque modèle porte son
# propre dépôt HuggingFace source au lieu de supposer un unique H3_HF_REPO
# global. Permet d'ajouter un futur dépôt tiers sans toucher au reste du
# projet — il suffit d'une nouvelle entrée de manifeste. Deux dépôts utilisés
# aujourd'hui (voir config.env) :
#   - H3_HF_REPO      (Comfy-Org/MiniMax-H3)      : max, balanced, VAE, text
#                                                    encoders
#   - H3_HF_REPO_INT4 (tsolful/Minimax_H3_INT4MixedConvRot) : palier light
#                                                    (INT4Q, voir décision
#                                                    ci-dessous)
#
# Historique des paliers (voir CHANGELOG.md pour le détail des décisions) :
#   - light    : passé de "pruned INT8 ConvRot" à "pruned INT4Q" (dépôt
#                tiers). INT4Q retenu plutôt qu'INT4BQ malgré une taille
#                légèrement supérieure (~18.5 Go vs ~15.9 Go) : INT4Q garde
#                ~73-75% des couches en INT8 contre ~39-47% pour INT4BQ,
#                donc une fidélité visuelle nettement plus proche du palier
#                balanced — critère explicitement prioritaire sur la taille.
#                Noms de fichiers en majuscules (INT4Q/INT4BQ) volontairement
#                conservés tels quels : ce sont les noms exacts publiés par
#                le dépôt tiers, pas une convention du projet.
#   - balanced : passé de "int8_convrot" (poids non pruned, ~34 Go +
#                text encoder int8_convrot ~27.1 Go) à "pruned_int8_convrot"
#                (~21 Go, anciennement utilisé par "light") + text encoder
#                nvfp4_awq (~15.7 Go, anciennement utilisé uniquement par
#                "light") — nouveau standard communauté adopté tel quel.
#                L'ancien text encoder qwen3vl_32b_minimax_h3_int8_convrot
#                n'est donc plus référencé par aucun palier.
#   - balanced (2026-08) : diffusion models repassés de "pruned_int8_convrot"
#                à "pruned_fp8_scaled" (~21 Go, taille non confirmée
#                individuellement — vérifiée dynamiquement au téléchargement,
#                voir lib/download.sh), à la demande explicite de
#                l'utilisateur. À noter, pour référence future : le README du
#                dépôt Comfy-Org/MiniMax-H3 documente fp8_scaled comme un
#                repli ("à utiliser seulement si int8_convrot n'est pas
#                utilisable"), pas comme une amélioration — changement fait
#                en connaissance de cause. Le Turbo LoRA
#                (MINIMAX_H3_TURBO_LORA_URL, voir config.env) a été converti
#                et validé spécifiquement contre le checkpoint pruned/
#                curve-form (int8_convrot) ; sa compatibilité avec
#                pruned_fp8_scaled n'est PAS vérifiée — risque assumé par
#                l'utilisateur pour MiniMax_H3_REF2V_TURBO_PLUS_SAGE*.json,
#                qui référence donc maintenant fp8_scaled lui aussi.
#   - max      : inchangé (BF16).
#   - 5 paliers (2026-08, révision) : le dépôt officiel Comfy-Org/MiniMax-H3
#                s'est enrichi entre-temps de fichiers "pruned_bf16" (pleine
#                précision, élagué — 40.2 Go, confirmé par la communauté) en
#                plus des "pruned_int8_convrot"/"pruned_fp8_scaled" déjà
#                connus. Plutôt que de garder "balanced" sur fp8_scaled (un
#                repli documenté comme tel par Comfy-Org, pas une
#                recommandation), le palier est éclaté en 5 pour donner un
#                choix explicite plutôt qu'un compromis caché :
#                  - light         : inchangé (INT4Q, dépôt tiers H3_HF_REPO_INT4)
#                  - pruned        : NOUVEAU — pruned_int8_convrot (21 Go,
#                                    recommandation officielle Comfy-Org),
#                                    redevient compatible Turbo LoRA (voir
#                                    note "balanced (2026-08)" ci-dessus —
#                                    validé contre ce même checkpoint).
#                  - pruned_scaled : NOUVEAU — pruned_fp8_scaled (21 Go),
#                                    repli explicite pour qui ne peut pas
#                                    utiliser int8_convrot ; n'est JAMAIS
#                                    choisi automatiquement par --tier=auto
#                                    (voir GPU_TIER_RECOMMENDED, lib/gpu.sh)
#                                    — toujours un choix manuel délibéré.
#                  - balanced      : redéfini — pruned_bf16 (40.2 Go),
#                                    pleine précision (aucune quantization
#                                    lossy sur le transformer), donc "les
#                                    modèles officiels" au sens strict.
#                  - max           : inchangé (BF16 non élagué).
#                Les 4 paliers autres que "light" viennent maintenant tous du
#                même dépôt officiel H3_HF_REPO (aucun dépôt tiers hors
#                "light").
#
# H3_DIFFUSION_FL2VA / H3_DIFFUSION_REF2VA / H3_TEXT_ENCODER ne sont jamais
# référencés par leur nom littéral plus bas dans ce fichier : ils sont passés
# en chaîne à _pick_for_tier, qui les résout via une nameref (`local -n`).
# Cette indirection n'est pas tracée par l'outil de lint, qui les marque donc
# à tort comme inutilisés (cf. H3_VAE ci-dessous, bien détecté car utilisé
# directement en "${H3_VAE[@]}").
# shellcheck disable=SC2034
H3_DIFFUSION_FL2VA=(
  "${H3_HF_REPO}|diffusion_models/minimax_h3_fl2va_bf16.safetensors|max|66.3"
  "${H3_HF_REPO}|diffusion_models/minimax_h3_fl2va_pruned_bf16.safetensors|balanced|40.2"
  "${H3_HF_REPO}|diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors|pruned|21"
  "${H3_HF_REPO}|diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors|pruned_scaled|21"
  "${H3_HF_REPO_INT4}|diffusion_models/minimax_h3_fl2va_pruned_INT4Q.safetensors|light|18.5"
)
# shellcheck disable=SC2034  # cf. note ci-dessus sur H3_DIFFUSION_FL2VA
H3_DIFFUSION_REF2VA=(
  "${H3_HF_REPO}|diffusion_models/minimax_h3_ref2va_bf16.safetensors|max|66.3"
  "${H3_HF_REPO}|diffusion_models/minimax_h3_ref2va_pruned_bf16.safetensors|balanced|40.2"
  "${H3_HF_REPO}|diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors|pruned|21"
  "${H3_HF_REPO}|diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors|pruned_scaled|21"
  "${H3_HF_REPO_INT4}|diffusion_models/minimax_h3_ref2va_pruned_INT4Q.safetensors|light|18.4"
)
# shellcheck disable=SC2034  # cf. note ci-dessus sur H3_DIFFUSION_FL2VA
H3_TEXT_ENCODER=(
  "${H3_HF_REPO}|text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors|max|51.5"
  "${H3_HF_REPO}|text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|balanced|15.7"
  "${H3_HF_REPO}|text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|pruned|15.7"
  "${H3_HF_REPO}|text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|pruned_scaled|15.7"
  "${H3_HF_REPO}|text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|light|15.7"
)
# H3_VAE porte désormais son repo par entrée ("repo|sous_chemin"), comme les
# autres manifestes — tier-indépendant (une seule entrée par composant,
# jamais résolue via _pick_for_tier).
H3_VAE=(
  "${H3_HF_REPO}|vae/minimax_h3_video_vae_fp16.safetensors"
  "${H3_HF_REPO}|vae/minimax_h3_audio_vae_fp32.safetensors"
)

_pick_for_tier() {
  # _pick_for_tier <tier> <array_name> -> écho "repo|subpath|size" sur stdout
  local tier="$1"; shift
  local -n arr="$1"
  local entry repo subpath t size
  for entry in "${arr[@]}"; do
    IFS='|' read -r repo subpath t size <<< "$entry"
    if [[ "$t" == "$tier" ]]; then echo "$repo|$subpath|$size"; return 0; fi
  done
  return 1
}

resolve_h3_tier() {
  local tier="$H3_TIER"
  if [[ "$tier" == "auto" ]]; then
    tier="${GPU_TIER_RECOMMENDED:-balanced}"
    log_info "$(t models_tier_auto_selected "$tier")"
  fi
  case "$tier" in
    light|pruned|pruned_scaled|balanced|max) ;;
    *)
      log_warn "$(t models_tier_unknown_fallback "$tier")"
      tier="balanced"
      ;;
  esac
  echo "$tier"
}

# resolve_h3_workflows — normalise H3_WORKFLOWS/--workflows= : étend "all" en
# "t2v,i2v,r2v", ignore les jetons inconnus (avec avertissement), et retombe
# sur "t2v,i2v,r2v" si la liste résultante est vide. Seule fonction du projet
# à interpréter cette variable — tout le reste (choix des poids à
# télécharger) en dérive via _workflow_needs() ci-dessous.
resolve_h3_workflows() {
  local raw="${H3_WORKFLOWS:-t2v,i2v,r2v}"
  raw="${raw/all/t2v,i2v,r2v}"

  local -a tokens=() valid=()
  IFS=',' read -ra tokens <<< "$raw"
  local t
  for t in "${tokens[@]}"; do
    case "$t" in
      t2v|i2v|r2v) valid+=("$t") ;;
      "") ;;
      *) log_warn "$(t models_workflow_unknown_token "$t")" ;;
    esac
  done

  if [[ ${#valid[@]} -eq 0 ]]; then
    log_warn "$(t models_workflows_none_valid "${H3_WORKFLOWS:-}")"
    valid=(t2v i2v r2v)
  fi

  local IFS=','
  echo "${valid[*]}"
}

# _workflow_needs <workflows_csv> <t2v|i2v|r2v...>
# Vrai si la liste de workflows résolue contient au moins un des workflows
# donnés. Utilisé pour dériver need_fl2va (t2v ou i2v) / need_ref2va (r2v) à
# un seul endroit, réutilisé par l'estimation de taille et le téléchargement.
_workflow_needs() {
  local workflows="$1"; shift
  local -a wf=()
  IFS=',' read -ra wf <<< "$workflows"
  local w want
  for w in "${wf[@]}"; do
    for want in "$@"; do
      [[ "$w" == "$want" ]] && return 0
    done
  done
  return 1
}

# build_h3_model_manifest <tier>
# Point d'entrée UNIQUE qui peuple H3_MODEL_FILES (sous-chemin) ET
# H3_MODEL_REPO (dépôt HuggingFace source) pour le palier donné, à partir du
# manifeste H3_DIFFUSION_FL2VA / H3_DIFFUSION_REF2VA / H3_TEXT_ENCODER /
# H3_VAE (seule source de vérité pour dépôts, chemins et tailles). À appeler
# avant collect_missing_models()/download_missing_models() — tout le reste
# du fichier lit H3_MODEL_FILES/H3_MODEL_REPO, plus aucune fonction ne
# rappelle _pick_for_tier() séparément pour fl2va/ref2va/text_encoder.
#
# Deux tableaux associatifs parallèles (plutôt qu'un seul encodé
# "repo::subpath") : chaque appelant existant qui lit H3_MODEL_FILES[key]
# pour un sous-chemin (basename, dirname, comparaison avec un JSON de
# workflow — voir lib/workflows.sh) continue de fonctionner sans changement ;
# seuls les appelants qui ont besoin du dépôt lisent H3_MODEL_REPO[key] en
# plus, à l'endroit précis où c'est nécessaire.
declare -A H3_MODEL_FILES=()
declare -A H3_MODEL_REPO=()
build_h3_model_manifest() {
  local tier="$1"
  local entry repo subpath size

  entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"
  IFS='|' read -r repo subpath size <<< "$entry"
  H3_MODEL_REPO[fl2va]="$repo"; H3_MODEL_FILES[fl2va]="$subpath"

  entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"
  IFS='|' read -r repo subpath size <<< "$entry"
  H3_MODEL_REPO[ref2va]="$repo"; H3_MODEL_FILES[ref2va]="$subpath"

  entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"
  IFS='|' read -r repo subpath size <<< "$entry"
  H3_MODEL_REPO[text_encoder]="$repo"; H3_MODEL_FILES[text_encoder]="$subpath"

  for entry in "${H3_VAE[@]}"; do
    IFS='|' read -r repo subpath <<< "$entry"
    case "$subpath" in
      *video_vae*) H3_MODEL_REPO[video_vae]="$repo"; H3_MODEL_FILES[video_vae]="$subpath" ;;
      *audio_vae*) H3_MODEL_REPO[audio_vae]="$repo"; H3_MODEL_FILES[audio_vae]="$subpath" ;;
    esac
  done
}

# Taille approximative (Go) des deux VAE — non tarifées par palier dans le
# manifeste (H3_VAE ne liste que les chemins) : elles ne varient pas selon le
# palier choisi.
H3_VAE_SIZE_GB_VIDEO="2.7"
H3_VAE_SIZE_GB_AUDIO="0.3"

# get_model_size_gb <key> <tier> -> taille approximative (Go) sur stdout.
# Réutilise le même manifeste de tailles que build_h3_model_manifest() —
# aucune duplication des chiffres eux-mêmes, juste un accès par clé de modèle
# plutôt que par liste de workflows.
get_model_size_gb() {
  local key="$1"
  local tier="$2"
  local entry

  case "$key" in
    fl2va)
      entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"; echo "${entry##*|}"
      ;;
    ref2va)
      entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"; echo "${entry##*|}"
      ;;
    text_encoder)
      entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"; echo "${entry##*|}"
      ;;
    video_vae)
      echo "$H3_VAE_SIZE_GB_VIDEO"
      ;;
    audio_vae)
      echo "$H3_VAE_SIZE_GB_AUDIO"
      ;;
  esac
}

# estimate_missing_download_size_gb <tier> <workflows_csv>
# Ne totalise que les modèles marqués manquants dans H3_MODEL_MISSING ET
# réellement requis par les workflows sélectionnés (fl2va pour t2v/i2v,
# ref2va pour r2v) — reflète donc le volume réellement téléchargé.
# Doit être appelée après collect_missing_models().
estimate_missing_download_size_gb() {
  local tier="$1"
  local workflows="$2"
  local total=0
  local key size

  for key in "${H3_MODEL_KEYS[@]}"; do
    [[ "$key" == "fl2va" ]] && ! _workflow_needs "$workflows" t2v i2v && continue
    [[ "$key" == "ref2va" ]] && ! _workflow_needs "$workflows" r2v && continue
    if [[ "${H3_MODEL_MISSING[$key]:-true}" == "true" ]]; then
      size="$(get_model_size_gb "$key" "$tier")"
      total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
    fi
  done

  echo "$total"
}

# --- Téléchargement CivitAI --------------------------------------------------
# download_civitai_model <url> <dest_path>
# - crée le dossier de destination si nécessaire
# - reprend un téléchargement interrompu (-C -)
# - suit les redirections (-L)
# - authentification optionnelle via CIVITAI_API_KEY (en-tête "Authorization:
#   Bearer ...") — même convention que install_lora.sh et
#   _download_preset_workflow_from_civitai() (lib/presets.sh). Jamais requise
#   pour un fichier public, mais INDISPENSABLE pour tout fichier marqué
#   NSFW/accès restreint sur CivitAI (ex. certains checkpoints communautaires
#   comme celui de PRESET_DASIWA_MMH3V12_CIVITAI_MODELS) — sans elle, CivitAI
#   répond 401 même avec une URL de téléchargement par ailleurs correcte.
# - suit le code HTTP réel (curl -w) plutôt que --fail seul, pour distinguer
#   une erreur d'authentification (401/403 — message dédié suggérant
#   CIVITAI_API_KEY) d'une erreur réseau/serveur classique (message générique,
#   déjà existant).
download_civitai_model() {
  local url="$1"
  local dest="$2"
  local dest_dir; dest_dir="$(dirname "$dest")"
  mkdir -p "$dest_dir"

  local -a auth_args=()
  [[ -n "${CIVITAI_API_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer ${CIVITAI_API_KEY}")

  local max_retries="${DOWNLOAD_MAX_RETRIES:-5}"
  local attempt=1 http_code=""

  announce_download "$(basename "$dest")"
  while (( attempt <= max_retries )); do
    http_code="$(curl -L -C - --retry 3 --retry-delay 2 "${auth_args[@]}" -o "$dest" -w '%{http_code}' "$url" 2>/dev/null || true)"

    if [[ "$http_code" =~ ^2 ]] && [[ -s "$dest" ]]; then
      log_ok "$(t models_civitai_downloaded "$(basename "$dest")")"
      return 0
    fi

    if [[ "$http_code" == "401" || "$http_code" == "403" ]]; then
      # Réponse d'erreur (souvent une page HTML/JSON courte), jamais un
      # fragment binaire valide : la supprimer pour ne pas fausser la
      # reprise -C - de la tentative suivante (ou d'un futur --only-models).
      rm -f -- "$dest" 2>/dev/null || true
      log_warn "$(t models_civitai_attempt_failed_auth "$attempt" "$max_retries" "$(basename "$dest")")"
    else
      log_warn "$(t models_civitai_attempt_failed "$attempt" "$max_retries" "$(basename "$dest")")"
    fi
    attempt=$((attempt + 1))
    [[ $attempt -le $max_retries ]] && sleep 2
  done

  if [[ "$http_code" == "401" || "$http_code" == "403" ]]; then
    log_error "$(t models_civitai_final_failed_auth "$url")"
  else
    log_error "$(t models_civitai_final_failed "$url")"
  fi
  return 1
}

# --- Dispatcher source pour les modèles de diffusion -------------------------
# download_diffusion_model <repo> <hf_subpath> <base_dir> <civitai_url> <source>
# Route vers HuggingFace (comportement historique, inchangé) ou CivitAI selon
# MODEL_SOURCE. <repo> vient de H3_MODEL_REPO[key] (résolu pour le palier
# actif par build_h3_model_manifest()) — plus de dépendance à H3_HF_REPO en
# dur ici, cf. architecture multi-dépôts. Le nom de fichier final est
# toujours identique quel que soit la source (dérivé du sous-chemin
# HuggingFace), donc aucun autre script (workflows, verify, etc.) n'a besoin
# de savoir d'où vient le fichier.
download_diffusion_model() {
  local repo="$1"
  local hf_subpath="$2"
  local base="$3"
  local civitai_url="$4"
  local source="$5"

  if [[ "$source" == "civitai" ]]; then
    local filename; filename="$(basename "$hf_subpath")"
    download_civitai_model "$civitai_url" "${base}/diffusion_models/${filename}"
  else
    # download_hf_file() délègue à `hf download --local-dir`, qui reconstruit
    # lui-même le sous-chemin complet du dépôt (diffusion_models/, text_encoders/,
    # vae/...) sous le dossier racine fourni — donc c'est bien la racine
    # models/ qu'il faut passer ici, pas le sous-dossier final (sous peine de
    # double dossier diffusion_models/diffusion_models/, bug corrigé ici).
    # mkdir -p reste utile en amont : garantit le sous-dossier même si l'outil
    # HF ne le crée pas lui-même.
    mkdir -p "${base}/$(dirname "$hf_subpath")"
    download_hf_file "$repo" "$hf_subpath" "$base"
  fi
}

# =============================================================================
# Détection locale des modèles déjà installés (aucun accès réseau ici)
# =============================================================================
#
# Chaque modèle final est décrit une seule fois : chemin relatif sous
# models/ (résolu pour le palier actif par build_h3_model_manifest(), voir
# plus haut — H3_MODEL_FILES n'est plus figé sur le palier "light"), taille
# minimale plausible (détecte un téléchargement coupé) et libellé affiché.
# C'est la seule source de vérité utilisée par le scan, la suppression des
# fichiers corrompus et le calcul des téléchargements manquants — donc
# aucune duplication entre ces étapes.

H3_MODEL_KEYS=(fl2va ref2va text_encoder video_vae audio_vae)

# Seuils volontairement conservateurs et valables pour les trois paliers
# (light/balanced/max sont tous très au-dessus de ces minimums) : ils ne
# servent qu'à repérer un téléchargement interrompu, pas à valider un
# checksum exact.
declare -A H3_MODEL_MIN_BYTES=(
  [fl2va]=$((15 * 1024 * 1024 * 1024))          # > 15 Go
  [ref2va]=$((15 * 1024 * 1024 * 1024))         # > 15 Go
  [text_encoder]=$((10 * 1024 * 1024 * 1024))   # > 10 Go
  [video_vae]=$((3 * 1024 * 1024 * 1024))       # > 3 Go
  [audio_vae]=$((300 * 1024 * 1024))            # > 300 Mo
)

declare -A H3_MODEL_LABELS=(
  [fl2va]="FL2VA"
  [ref2va]="REF2VA"
  [text_encoder]="Text Encoder"
  [video_vae]="Video VAE"
  [audio_vae]="Audio VAE"
)

# État calculé par collect_missing_models() : "true"/"false" par clé.
declare -A H3_MODEL_MISSING=()

# --- Vérification d'intégrité SHA256 (optionnelle) --------------------------
#
# Deux sources possibles pour le SHA256 attendu d'un modèle, dans cet ordre :
#   1) MODEL_SHA256[key] — surcharge manuelle définie dans config.env
#      (tableau déjà déclaré là-bas, vide par défaut). Aucun accès réseau :
#      utilisée telle quelle si renseignée.
#   2) Si H3_VERIFY_SHA256_ONLINE=true, tentative de récupération du SHA256
#      publié par HuggingFace (en-tête X-Linked-Etag des fichiers LFS).
#      Coûte une requête réseau par modèle : désactivé par défaut pour
#      préserver un second lancement instantané et sans réseau.
# Si aucun SHA256 n'est disponible (cas par défaut), la validation retombe
# sur le contrôle de taille minimale déjà en place — comportement identique
# à avant l'ajout de cette fonctionnalité.
H3_VERIFY_SHA256_ONLINE="${H3_VERIFY_SHA256_ONLINE:-false}"

# MODEL_SHA256 est normalement déclaré dans config.env ; on se prémunit ici
# au cas où ce fichier serait chargé seul (tests, usage isolé du module).
if ! declare -p MODEL_SHA256 &>/dev/null; then
  declare -A MODEL_SHA256=()
fi

# compute_sha256 <path> -> empreinte SHA256 locale sur stdout.
# Retourne 1 si aucun outil de hachage n'est disponible sur le système.
compute_sha256() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  else
    return 1
  fi
}

# fetch_hf_sha256 <repo> <subpath> -> SHA256 publié par HuggingFace sur
# stdout, lu depuis l'en-tête X-Linked-Etag renvoyé pour les fichiers LFS.
# Retourne 1 si l'en-tête est absent (fichier non-LFS, erreur réseau, etc.).
fetch_hf_sha256() {
  local repo="$1"
  local subpath="$2"
  local url="https://huggingface.co/${repo}/resolve/main/${subpath}"
  local header sha

  header="$(curl -sIL --max-time 10 "$url" 2>/dev/null | tr -d '\r' | grep -i '^x-linked-etag:' | tail -n1)"
  [[ -z "$header" ]] && return 1
  sha="$(echo "$header" | grep -oE '[a-f0-9]{64}')"
  [[ -z "$sha" ]] && return 1
  echo "$sha"
}

# get_expected_sha256 <key> <repo> <hf_subpath> -> SHA256 attendu sur stdout,
# ou rien (et code 1) si aucun n'est disponible. Voir priorité ci-dessus.
# <repo> vient de H3_MODEL_REPO[key] côté appelant (architecture
# multi-dépôts) — plus de dépendance à H3_HF_REPO en dur ici.
get_expected_sha256() {
  local key="$1"
  local repo="$2"
  local hf_subpath="$3"

  if [[ -n "${MODEL_SHA256[$key]:-}" ]]; then
    echo "${MODEL_SHA256[$key]}"
    return 0
  fi

  if [[ "$H3_VERIFY_SHA256_ONLINE" == "true" ]]; then
    fetch_hf_sha256 "$repo" "$hf_subpath"
    return $?
  fi

  return 1
}

# model_file_size <path> -> taille en octets sur stdout, 0 si absent/illisible.
model_file_size() {
  local path="$1"
  [[ -e "$path" ]] || { echo 0; return 1; }
  stat -c%s "$path" 2>/dev/null || stat -f%z "$path" 2>/dev/null || wc -c < "$path" 2>/dev/null || echo 0
}

# model_exists <path>
model_exists() {
  [[ -f "$1" ]]
}

# model_is_valid <path> <min_bytes> [expected_sha256]
# Vrai si le fichier existe, fait plus de 0 octet, et atteint la taille
# minimale plausible pour ce modèle (détecte un téléchargement interrompu).
# Si un SHA256 attendu est fourni, il est vérifié en plus (et prime sur le
# résultat du seul contrôle de taille) ; sinon la taille suffit, comme avant.
model_is_valid() {
  local path="$1"
  local min_bytes="$2"
  local expected_sha256="${3:-}"
  local size

  model_exists "$path" || return 1
  size="$(model_file_size "$path")"
  [[ "$size" -gt 0 ]] || return 1
  if [[ -n "$min_bytes" && "$size" -lt "$min_bytes" ]]; then
    return 1
  fi

  if [[ -n "$expected_sha256" ]]; then
    local actual_sha256
    if actual_sha256="$(compute_sha256 "$path")"; then
      [[ "$actual_sha256" == "$expected_sha256" ]] || return 1
    else
      log_warn "$(t models_sha256_tool_missing "$(basename "$path")")"
    fi
  fi

  return 0
}

# remove_corrupted_model <path> — supprime un fichier absent/vide/incomplet
# après l'avoir signalé.
remove_corrupted_model() {
  local path="$1"
  echo "Corrupted or incomplete model detected:"
  basename "$path"
  rm -f "$path"
}

# collect_missing_models <base_dir>
# Étapes 1 à 3 : scanne les 5 modèles H3 sur disque, sans toucher au réseau.
# Supprime automatiquement tout fichier absent/vide/incomplet et le marque
# comme manquant. Remplit l'état global H3_MODEL_MISSING et affiche le
# récapitulatif ✓/✗ attendu par l'utilisateur.
collect_missing_models() {
  local base="$1"
  local key path full_path min_bytes label expected_sha256

  echo "------------------------------------------------"
  echo "Checking installed MiniMax H3 models..."

  for key in "${H3_MODEL_KEYS[@]}"; do
    path="${H3_MODEL_FILES[$key]}"
    full_path="${base}/${path}"
    min_bytes="${H3_MODEL_MIN_BYTES[$key]}"
    label="${H3_MODEL_LABELS[$key]}"
    # $path est déjà le sous-chemin relatif au dépôt HF (identique à
    # H3_MODEL_FILES), donc réutilisable tel quel pour un éventuel fetch.
    # H3_MODEL_REPO[$key] fournit le dépôt correspondant (architecture
    # multi-dépôts) — résolu par build_h3_model_manifest() pour le palier
    # actif, appelée avant collect_missing_models().
    expected_sha256="$(get_expected_sha256 "$key" "${H3_MODEL_REPO[$key]}" "$path" || true)"

    if model_is_valid "$full_path" "$min_bytes" "$expected_sha256"; then
      echo "✓ ${label}"
      H3_MODEL_MISSING[$key]="false"
    else
      # Fichier présent mais invalide (0 octet / incomplet) -> nettoyage.
      if [[ -e "$full_path" ]]; then
        remove_corrupted_model "$full_path"
      fi
      echo "✗ ${label}"
      H3_MODEL_MISSING[$key]="true"
    fi
  done
}

# any_model_missing <workflows_csv>
# Vrai si au moins un modèle RÉELLEMENT REQUIS par les workflows sélectionnés
# doit être téléchargé (fl2va n'est requis que pour t2v/i2v, ref2va que pour
# r2v — Text Encoder et VAE sont toujours requis).
any_model_missing() {
  local workflows="$1"
  local key
  for key in "${H3_MODEL_KEYS[@]}"; do
    [[ "$key" == "fl2va" ]] && ! _workflow_needs "$workflows" t2v i2v && continue
    [[ "$key" == "ref2va" ]] && ! _workflow_needs "$workflows" r2v && continue
    [[ "${H3_MODEL_MISSING[$key]:-true}" == "true" ]] && return 0
  done
  return 1
}

# h3_required_repos <workflows_csv> -> liste dédoublonnée (une par ligne, via
# echo "${arr[*]}") des dépôts HuggingFace réellement nécessaires : modèles
# manquants (H3_MODEL_MISSING) ET requis par les workflows sélectionnés
# (fl2va pour t2v/i2v, ref2va pour r2v — text encoder et VAE toujours
# requis). Seule fonction qui décide "quels dépôts contacter" pour la
# vérification de licence — hf_check_required_access() (lib/huggingface.sh)
# n'a plus besoin de connaître H3_HF_REPO/H3_HF_REPO_INT4 individuellement.
h3_required_repos() {
  local workflows="$1"
  local -A seen=()
  local -a repos=()
  local key repo

  for key in "${H3_MODEL_KEYS[@]}"; do
    [[ "$key" == "fl2va" ]] && ! _workflow_needs "$workflows" t2v i2v && continue
    [[ "$key" == "ref2va" ]] && ! _workflow_needs "$workflows" r2v && continue
    [[ "${H3_MODEL_MISSING[$key]:-true}" == "true" ]] || continue
    repo="${H3_MODEL_REPO[$key]:-}"
    [[ -n "$repo" && -z "${seen[$repo]:-}" ]] && { seen[$repo]=1; repos+=("$repo"); }
  done

  echo "${repos[*]}"
}

# need_hf_download <model_source> <workflows_csv>
# Vrai si au moins un téléchargement HuggingFace est réellement nécessaire :
# Text Encoder / VAE manquants (toujours servis par HF), ou modèles de
# diffusion manquants ET requis par les workflows sélectionnés alors que
# MODEL_SOURCE=huggingface.
need_hf_download() {
  local source="$1"
  local workflows="$2"

  [[ "${H3_MODEL_MISSING[text_encoder]}" == "true" ]] && return 0
  [[ "${H3_MODEL_MISSING[video_vae]}" == "true" ]] && return 0
  [[ "${H3_MODEL_MISSING[audio_vae]}" == "true" ]] && return 0

  if [[ "$source" != "civitai" ]]; then
    _workflow_needs "$workflows" t2v i2v && [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]] && return 0
    _workflow_needs "$workflows" r2v && [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]] && return 0
  fi
  return 1
}

# need_civitai_download <model_source> <workflows_csv>
# Vrai si au moins un modèle de diffusion manquant ET requis par les
# workflows sélectionnés doit venir de CivitAI.
need_civitai_download() {
  local source="$1"
  local workflows="$2"

  [[ "$source" == "civitai" ]] || return 1
  _workflow_needs "$workflows" t2v i2v && [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]] && return 0
  _workflow_needs "$workflows" r2v && [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]] && return 0
  return 1
}

# download_missing_models <base_dir> <model_source> <workflows_csv>
# Ne télécharge que les modèles marqués manquants dans H3_MODEL_MISSING ET
# requis par les workflows sélectionnés. N'appelle hf_check_required_access()
# que si need_hf_download() est vrai, et ne lance jamais curl (CivitAI) si les
# modèles de diffusion sont déjà valides ou pas requis.
# Les chemins téléchargés viennent uniquement de H3_MODEL_FILES (déjà résolu
# pour le palier actif par build_h3_model_manifest()) — aucun second appel à
# _pick_for_tier() ici, pour ne garder qu'un seul endroit qui décide "quel
# fichier pour quel palier".
download_missing_models() {
  local base="$1"
  local model_source="$2"
  local workflows="$3"

  if ! any_model_missing "$workflows"; then
    echo "------------------------------------------------"
    echo "All required MiniMax H3 models are already installed."
    echo "Skipping all downloads."
    echo "Skipping HuggingFace access check."
    echo "Continuing installation..."
    echo "------------------------------------------------"
    return 0
  fi

  if need_hf_download "$model_source" "$workflows"; then
    local -a required_repos=()
    # shellcheck disable=SC2207
    required_repos=($(h3_required_repos "$workflows"))
    if ! hf_check_required_access "${required_repos[@]}"; then
      log_error "$(t models_license_access_error1)"
      log_error "$(t models_license_access_error2)"
      return 1
    fi
  fi

  # Compteur global "[i/N]" affiché par announce_download() (lib/utils.sh) —
  # calculé une seule fois ici, sur le nombre réel de fichiers qui vont être
  # téléchargés dans cette série (modèles manquants ET requis par les
  # workflows sélectionnés, cf. any_model_missing()/H3_MODEL_MISSING plus
  # haut). Remise à zéro de l'index à chaque appel de download_missing_models()
  # pour ne pas hériter d'un compteur d'une série précédente (ex. relance
  # après échec partiel).
  local dl_total=0
  _workflow_needs "$workflows" t2v i2v && [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]] && dl_total=$((dl_total + 1))
  _workflow_needs "$workflows" r2v && [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]] && dl_total=$((dl_total + 1))
  [[ "${H3_MODEL_MISSING[text_encoder]}" == "true" ]] && dl_total=$((dl_total + 1))
  [[ "${H3_MODEL_MISSING[video_vae]}" == "true" ]] && dl_total=$((dl_total + 1))
  [[ "${H3_MODEL_MISSING[audio_vae]}" == "true" ]] && dl_total=$((dl_total + 1))
  # Exportées : lues par announce_download() dans lib/utils.sh, pas dans ce
  # fichier — voir le commentaire sur leur déclaration dans lib/utils.sh.
  export DOWNLOAD_FILE_TOTAL="$dl_total"
  export DOWNLOAD_FILE_INDEX=0

  if _workflow_needs "$workflows" t2v i2v && [[ "${H3_MODEL_MISSING[fl2va]}" == "true" ]]; then
    download_diffusion_model "${H3_MODEL_REPO[fl2va]}" "${H3_MODEL_FILES[fl2va]}" "$base" "$H3_CIVITAI_FL2VA_URL" "$model_source"
  fi

  if _workflow_needs "$workflows" r2v && [[ "${H3_MODEL_MISSING[ref2va]}" == "true" ]]; then
    download_diffusion_model "${H3_MODEL_REPO[ref2va]}" "${H3_MODEL_FILES[ref2va]}" "$base" "$H3_CIVITAI_REF2VA_URL" "$model_source"
  fi

  # Text Encoder et VAE restent toujours servis par HuggingFace, quel que
  # soit MODEL_SOURCE (CivitAI ne les propose pas), et sont toujours requis
  # quels que soient les workflows sélectionnés. Repo résolu individuellement
  # par clé (H3_MODEL_REPO) plutôt que le H3_HF_REPO global — les trois
  # viennent aujourd'hui du même dépôt Comfy-Org, mais rien ici ne le suppose
  # plus en dur.
  # download_hf_file() reçoit ici la racine models/ (voir download_diffusion_model()
  # ci-dessus) : `hf download --local-dir` reconstruit lui-même le sous-dossier
  # (text_encoders/, vae/...), mkdir -p le garantit en amont par sécurité.
  if [[ "${H3_MODEL_MISSING[text_encoder]}" == "true" ]]; then
    mkdir -p "${base}/$(dirname "${H3_MODEL_FILES[text_encoder]}")"
    download_hf_file "${H3_MODEL_REPO[text_encoder]}" "${H3_MODEL_FILES[text_encoder]}" "$base"
  fi

  if [[ "${H3_MODEL_MISSING[video_vae]}" == "true" ]]; then
    mkdir -p "${base}/$(dirname "${H3_MODEL_FILES[video_vae]}")"
    download_hf_file "${H3_MODEL_REPO[video_vae]}" "${H3_MODEL_FILES[video_vae]}" "$base"
  fi

  if [[ "${H3_MODEL_MISSING[audio_vae]}" == "true" ]]; then
    mkdir -p "${base}/$(dirname "${H3_MODEL_FILES[audio_vae]}")"
    download_hf_file "${H3_MODEL_REPO[audio_vae]}" "${H3_MODEL_FILES[audio_vae]}" "$base"
  fi

  echo "------------------------------------------------"
  return 0
}

download_h3_models() {
  log_step "$(t models_download_step)"

  # Palier et workflows résolus depuis H3_TIER/--tier= et H3_WORKFLOWS/
  # --workflows= (config.env, surchargeables en ligne de commande) — seule
  # source de vérité pour "quel fichier pour quel GPU" et "quels UNet sont
  # requis". build_h3_model_manifest() peuple H3_MODEL_FILES pour CE palier ;
  # tout le reste de ce fichier (scan disque, estimation, téléchargement) lit
  # H3_MODEL_FILES et n'a plus besoin de connaître le palier directement.
  local tier; tier="$(resolve_h3_tier)"
  local workflows; workflows="$(resolve_h3_workflows)"
  build_h3_model_manifest "$tier"

  local model_source="${MODEL_SOURCE:-huggingface}"
  case "$model_source" in
    huggingface|civitai) ;;
    *)
      log_warn "$(t models_source_unknown "$model_source")"
      model_source="huggingface"
      ;;
  esac

  # CivitAI n'héberge que les poids "pruned INT8 ConvRot"
  # (H3_CIVITAI_FL2VA_URL / H3_CIVITAI_REF2VA_URL, voir config.env). Depuis
  # 2026-08, le palier "balanced" utilise "pruned_fp8_scaled" (à la demande
  # explicite de l'utilisateur — voir historique ci-dessus), donc plus aucun
  # palier ne correspond aux fichiers CivitAI : téléchargement refusé
  # explicitement plutôt que de récupérer un int8_convrot sous un nom de
  # fichier fp8_scaled (mismatch silencieux sinon, jamais vérifié par
  # verify_local_file() puisqu'aucun hash SHA256 n'est renseigné par
  # défaut). Repli : MODEL_SOURCE=huggingface (défaut).
  if [[ "$model_source" == "civitai" ]]; then
    log_error "$(t models_civitai_deprecated_1)"
    log_error "$(t models_civitai_deprecated_2)"
    log_error "$(t models_civitai_deprecated_3)"
    log_error "$(t models_civitai_deprecated_4)"
    log_error "$(t models_civitai_deprecated_5)"
    return 1
  fi

  echo "------------------------------------------------"
  if [[ "$model_source" == "civitai" ]]; then
    echo "Selected Model Source : CivitAI"
  else
    echo "Selected Model Source : HuggingFace"
  fi
  log_info "$(t models_tier_workflows_selected "$tier" "$workflows")"

  local base="${INSTALL_DIR}/models"

  # --- Étapes 1-3 : scan disque local, réparation, aucun accès réseau ------
  collect_missing_models "$base"

  # --- Rien à faire ? download_missing_models() gère elle-même le cas "tout
  #     est déjà installé" (message + retour rapide) — pas besoin de le
  #     revérifier ici, un seul endroit décide de ce cas. ------------------
  if ! any_model_missing "$workflows"; then
    download_missing_models "$base" "$model_source" "$workflows"
    log_ok "$(t models_already_installed "$tier" "$workflows" "$model_source")"
    return 0
  fi

  # --- Au moins un modèle requis manque -> estimation espace disque avant
  #     de contacter le réseau, puis téléchargement ciblé -------------------
  # Estimation basée uniquement sur les modèles manquants ET requis par les
  # workflows sélectionnés : si seul le Video VAE manque, on annonce
  # ~2.7 Go et non ~60 Go ; si seul r2v est sélectionné, fl2va n'est jamais
  # compté même s'il est absent du disque.
  local est_gb; est_gb="$(estimate_missing_download_size_gb "$tier" "$workflows")"
  local free_gb; free_gb="$(free_disk_gb "$INSTALL_DIR")"
  log_info "$(t models_space_required_estimate "$est_gb" "${free_gb:-$(t gpu_cuda_unknown)}")"

  if [[ -n "$free_gb" ]] && awk -v f="$free_gb" -v e="$est_gb" 'BEGIN{exit !(f < e)}'; then
    log_warn "$(t models_disk_maybe_insufficient "$free_gb" "$est_gb")"
    confirm "$(t models_confirm_continue_anyway)" || { log_error "$(t models_download_cancelled_by_user)"; return 1; }
  fi

  download_missing_models "$base" "$model_source" "$workflows" || return 1

  log_ok "$(t models_downloaded_done "$tier" "$workflows" "$model_source")"
}
