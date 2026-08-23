#!/usr/bin/env bash
# install_lora.sh — gestionnaire de LoRA pour ComfyUI (MiniMax H3).
#
# Installe un LoRA depuis une URL directe dans models/loras/ (ou
# models/loras/personal/ avec --personal), liste les LoRA déjà installés,
# ou en supprime un. Sources officiellement supportées :
#   - Hugging Face  (ex: https://huggingface.co/.../resolve/main/mylora.safetensors)
#   - CivitAI       (https://civitai.com/... et https://civitai.red/...)
#   - toute URL directe pointant vers un fichier .safetensors
# Aucune authentification n'est requise pour les dépôts/LoRA publics sur ces
# trois sources.
#
# --personal :
#   Installe (ou liste/supprime) dans models/loras/personal/ au lieu de
#   models/loras/ — ce sous-dossier est le SEUL que lib/personal_storage.sh
#   sauvegarde automatiquement vers votre coffre HF perso (voir
#   PERSONAL_STORAGE_HF_REPO, config.env, et sync_push.sh). Sans ce flag,
#   comportement historique inchangé : installation dans models/loras/ (là
#   où atterrit aussi le Turbo LoRA officiel MiniMax H3), jamais sauvegardé
#   automatiquement. ComfyUI scanne les deux dossiers de la même façon
#   (récursif) : un LoRA dans models/loras/personal/ reste normalement
#   sélectionnable dans l'interface, où qu'il soit.
#
# Authentification CivitAI (optionnelle) :
#   Si la variable d'environnement CIVITAI_API_KEY est définie et non vide,
#   elle est automatiquement envoyée en en-tête "Authorization: Bearer ..."
#   sur les requêtes vers civitai.com / civitai.red (uniquement celles-ci —
#   jamais vers Hugging Face ou une autre URL). Pratique sur RunPod, où la
#   clé peut être définie une fois pour toutes dans le template du pod. Sans
#   cette variable, le comportement est strictement identique à avant :
#   téléchargement anonyme, aucun avertissement.
#
# Usage :
#   ./install_lora.sh <URL>                    installe (ou saute si déjà présent)
#   ./install_lora.sh --personal <URL>          installe dans models/loras/personal/ (sauvegardé par sync_push.sh)
#   ./install_lora.sh --force <URL>             réinstalle même si déjà présent
#   ./install_lora.sh --filename <nom> <URL>    force un nom local explicite
#   ./install_lora.sh --list                    liste les LoRA installés (avec taille)
#   ./install_lora.sh --list --personal         idem, mais dans models/loras/personal/ uniquement
#   ./install_lora.sh --remove <fichier>        supprime un LoRA (confirmation demandée)
#   ./install_lora.sh --remove --personal <fichier>  idem, dans models/loras/personal/
#   ./install_lora.sh --help                    aide détaillée
#
# Script utilitaire additionnel : il ne modifie ni config.env, ni install.sh,
# ni le flux d'installation existant (lib/*.sh non touchés, y compris
# lib/personal_storage.sh — --personal se contente d'écrire dans le dossier
# que cette dernière sait déjà lire). Il n'a besoin d'aucun outil/SDK
# Hugging Face (pas de `hf`/`huggingface-cli`, pas de token) — uniquement
# curl (et python3, en option, pour décoder les URL).

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
[[ -f "${PROJECT_ROOT}/config.env" ]] && source "${PROJECT_ROOT}/config.env"

# shellcheck disable=SC1091
if [[ -f "${PROJECT_ROOT}/lib/utils.sh" ]]; then
  # shellcheck disable=SC1091
  source "${PROJECT_ROOT}/lib/utils.sh"
else
  # Repli minimal si lib/utils.sh est absent, pour que ce script reste autonome.
  log_info()  { echo -e "[INFO] $*"; }
  log_ok()    { echo -e "[ OK ] $*"; }
  log_warn()  { echo -e "[WARN] $*" >&2; }
  log_error() { echo -e "[FAIL] $*" >&2; }
  # shellcheck disable=SC1091
  if [[ -f "${PROJECT_ROOT}/lib/i18n.sh" ]]; then
    source "${PROJECT_ROOT}/lib/i18n.sh"
  else
    t() { local key="$1"; shift || true; printf '%s' "$key"; }
    techo() { local key="$1"; shift || true; echo "$key"; }
  fi
fi

INSTALL_DIR="${INSTALL_DIR:-/workspace/ComfyUI}"
LORA_DIR="${INSTALL_DIR}/models/loras"
# Même sous-dossier que lib/personal_storage.sh::PERSONAL_STORAGE_LORAS_DIR()
# — ne pas diverger de ce chemin, c'est ce que sync_push.sh sauvegarde.
PERSONAL_LORA_DIR="${LORA_DIR}/personal"
# Même sous-dossier que lib/personal_storage.sh::PERSONAL_STORAGE_MANIFEST_LORAS_DIR()
# — cible de --manifest ci-dessous, utilisée par
# _personal_storage_process_manifest_loras() pour installer les LoRA listés
# dans loras_manifest.txt (coffre HF perso). Dossier séparé de
# PERSONAL_LORA_DIR : contenu déclaratif re-téléchargé à chaque restauration,
# jamais lui-même sauvegardé vers le coffre HF (voir sync_push.sh).
MANIFEST_LORA_DIR="${LORA_DIR}/manifest"
DOWNLOAD_MAX_RETRIES="${DOWNLOAD_MAX_RETRIES:-5}"

# usage
# Le texte d'aide (--help) est volumineux et fortement interpolé (chemins de
# dossiers, nom du script) : gardé sous forme de deux heredocs FR/EN séparés
# plutôt que dans lib/lang/*.sh, pour éviter la fragilité d'un printf à
# vingt-et-quelques positions %s qui se désynchroniseraient au moindre futur
# ajout de ligne dans une seule des deux langues.
usage() {
  if [[ "${INSTALLER_LANG:-en}" == "fr" ]]; then
    cat <<EOF
Usage : $0 [OPTIONS] <URL>

Gestionnaire de LoRA pour ComfyUI — installe, liste ou supprime les LoRA
présents dans :
  ${LORA_DIR}
  ${PERSONAL_LORA_DIR}  (avec --personal)
  ${MANIFEST_LORA_DIR}  (avec --manifest)

Sources supportées (aucune authentification requise pour du contenu public) :
  - Hugging Face : lien direct .../resolve/main/fichier.safetensors
  - CivitAI      : civitai.com et civitai.red
  - toute URL directe pointant vers un fichier .safetensors

--personal :
  Installe (ou liste/supprime) dans ${PERSONAL_LORA_DIR}
  au lieu de ${LORA_DIR} — c'est le SEUL sous-dossier
  sauvegardé automatiquement vers votre coffre HF perso (voir
  PERSONAL_STORAGE_HF_REPO dans config.env, et sync_push.sh). Utilisez ce
  flag pour tout LoRA que vous voulez retrouver sur votre prochain pod sans
  le retélécharger. Combinable avec --force, --filename, --list, --remove.

--manifest :
  Installe (ou liste/supprime) dans ${MANIFEST_LORA_DIR}
  au lieu de ${LORA_DIR} — c'est le dossier utilisé en interne par
  lib/personal_storage.sh pour traiter loras_manifest.txt (coffre HF perso) :
  contenu déclaratif, re-téléchargé à chaque restauration, jamais lui-même
  sauvegardé vers le coffre HF. Mutuellement exclusif avec --personal.
  Combinable avec --force, --filename, --list, --remove.

  Exemple :
    $0 --manifest https://civitai.com/api/download/models/123456

Authentification CivitAI (optionnelle) :
  Si la variable d'environnement CIVITAI_API_KEY est définie, elle est
  utilisée automatiquement pour les téléchargements CivitAI/civitai.red
  (nécessaire pour certains LoRA restreints). Aucun effet sur les autres
  sources. Rien à faire si elle n'est pas définie.

      Exemple :
        CIVITAI_API_KEY=xxxxx $0 https://civitai.com/api/download/models/123456

Installation :
  $0 <URL>
      Installe le LoRA depuis <URL>. Si le fichier est déjà installé, le
      téléchargement est sauté ("LoRA déjà installé.").

      Exemples :
        $0 https://huggingface.co/username/repo/resolve/main/mylora.safetensors
        $0 https://civitai.com/api/download/models/123456
        $0 https://civitai.red/api/download/models/3193337?fileId=3074134

  $0 --personal <URL>
      Installe dans ${PERSONAL_LORA_DIR} — sauvegardé
      automatiquement par sync_push.sh / au prochain "update.sh" (voir
      PERSONAL_STORAGE_HF_REPO, config.env).

      Exemple :
        $0 --personal https://civitai.com/api/download/models/123456

  $0 --force <URL>
      Force le retéléchargement même si le LoRA est déjà installé.

      Exemple :
        $0 --force https://civitai.com/api/download/models/123456

  $0 --filename <nom.safetensors> <URL>
      Impose le nom local du fichier dans le dossier cible, au lieu du nom
      déduit de l'URL / de l'en-tête Content-Disposition. Combinable avec
      --force et --personal. L'extension .safetensors est garantie même si
      omise.

      Exemple :
        $0 --filename my_turbo_lora.safetensors https://civitai.com/api/download/models/123456

Consultation :
  $0 --list [--personal]
      Affiche tous les LoRA installés dans le dossier concerné, numérotés,
      avec la taille de chacun (MB, ou GB au-delà de 1 Go) ainsi que le
      nombre total et la taille totale.

      Exemples :
        $0 --list
        $0 --list --personal

Suppression :
  $0 --remove [--personal] <fichier.safetensors>
      Supprime un LoRA précis du dossier concerné (confirmation demandée).
      Ne supprime jamais le dossier lui-même, uniquement le fichier indiqué.

      Exemples :
        $0 --remove anime_style.safetensors
        $0 --remove --personal H3-GalaxyAce.safetensors

Autres :
  -h, --help
      Affiche cette aide.
EOF
  else
    cat <<EOF
Usage: $0 [OPTIONS] <URL>

LoRA manager for ComfyUI — installs, lists, or removes LoRAs found in:
  ${LORA_DIR}
  ${PERSONAL_LORA_DIR}  (with --personal)
  ${MANIFEST_LORA_DIR}  (with --manifest)

Supported sources (no authentication required for public content):
  - Hugging Face: direct link .../resolve/main/file.safetensors
  - CivitAI     : civitai.com and civitai.red
  - any direct URL pointing to a .safetensors file

--personal:
  Installs (or lists/removes) in ${PERSONAL_LORA_DIR}
  instead of ${LORA_DIR} — this is the ONLY subfolder
  automatically backed up to your personal HF vault (see
  PERSONAL_STORAGE_HF_REPO in config.env, and sync_push.sh). Use this flag
  for any LoRA you want to find again on your next pod without
  redownloading it. Combinable with --force, --filename, --list, --remove.

--manifest:
  Installs (or lists/removes) in ${MANIFEST_LORA_DIR}
  instead of ${LORA_DIR} — this is the folder used internally by
  lib/personal_storage.sh to process loras_manifest.txt (personal HF vault):
  declarative content, re-downloaded on every restore, never itself backed
  up to the HF vault. Mutually exclusive with --personal.
  Combinable with --force, --filename, --list, --remove.

  Example:
    $0 --manifest https://civitai.com/api/download/models/123456

CivitAI authentication (optional):
  If the CIVITAI_API_KEY environment variable is set, it's automatically
  used for CivitAI/civitai.red downloads (required for some restricted
  LoRAs). No effect on other sources. Nothing to do if it isn't set.

      Example:
        CIVITAI_API_KEY=xxxxx $0 https://civitai.com/api/download/models/123456

Install:
  $0 <URL>
      Installs the LoRA from <URL>. If the file is already installed, the
      download is skipped ("LoRA already installed.").

      Examples:
        $0 https://huggingface.co/username/repo/resolve/main/mylora.safetensors
        $0 https://civitai.com/api/download/models/123456
        $0 https://civitai.red/api/download/models/3193337?fileId=3074134

  $0 --personal <URL>
      Installs in ${PERSONAL_LORA_DIR} — automatically
      backed up by sync_push.sh / on the next "update.sh" (see
      PERSONAL_STORAGE_HF_REPO, config.env).

      Example:
        $0 --personal https://civitai.com/api/download/models/123456

  $0 --force <URL>
      Forces a redownload even if the LoRA is already installed.

      Example:
        $0 --force https://civitai.com/api/download/models/123456

  $0 --filename <name.safetensors> <URL>
      Forces the local filename in the target folder, instead of the name
      inferred from the URL / Content-Disposition header. Combinable with
      --force and --personal. The .safetensors extension is guaranteed even
      if omitted.

      Example:
        $0 --filename my_turbo_lora.safetensors https://civitai.com/api/download/models/123456

Browse:
  $0 --list [--personal]
      Shows every LoRA installed in the relevant folder, numbered, with the
      size of each (MB, or GB past 1 GB) plus the total count and total
      size.

      Examples:
        $0 --list
        $0 --list --personal

Remove:
  $0 --remove [--personal] <file.safetensors>
      Removes a specific LoRA from the relevant folder (confirmation
      required). Never removes the folder itself, only the given file.

      Examples:
        $0 --remove anime_style.safetensors
        $0 --remove --personal H3-GalaxyAce.safetensors

Other:
  -h, --help
      Shows this help.
EOF
  fi
}

check_comfyui_installed() {
  if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
    log_error "$(t install_comfyui_missing "$INSTALL_DIR")"
    exit 1
  fi
}

create_lora_folder() {
  local dir="$1"
  if [[ ! -d "$dir" ]]; then
    log_info "$(t loracli_creating_folder "$dir")"
    mkdir -p "$dir"
  fi
}

# is_civitai_url <url>
# Vrai si l'URL cible civitai.com ou civitai.red.
is_civitai_url() {
  local url="$1"
  [[ "$url" == *"civitai.com"* || "$url" == *"civitai.red"* ]]
}

# civitai_auth_curl_args <url>
# Si CIVITAI_API_KEY est définie et non vide, ET que l'URL cible CivitAI,
# affiche les arguments curl "-H Authorization: Bearer ..." à ajouter (un
# argument par ligne, à charger avec `mapfile`/`readarray`). Sinon n'affiche
# rien. La clé n'est jamais envoyée à une autre destination (Hugging Face,
# lien direct, etc.) et n'est jamais journalisée.
civitai_auth_curl_args() {
  local url="$1"
  if [[ -n "${CIVITAI_API_KEY:-}" ]] && is_civitai_url "$url"; then
    printf '%s\n' "-H" "Authorization: Bearer ${CIVITAI_API_KEY}"
  fi
}

# determine_lora_filename <url> [override]
# Détermine le nom de fichier local le plus fiable possible :
#   0) [override] non vide -> utilisé tel quel (priorité absolue, aucune
#      requête réseau nécessaire pour le déterminer)
#   1) URL se terminant directement par .safetensors -> on garde ce nom
#   2) CivitAI (civitai.com / civitai.red) -> le vrai nom est dans l'en-tête
#      Content-Disposition renvoyé après redirection, pas dans l'URL
#   3) Repli -> dernier segment de l'URL, sinon nom générique
# Dans tous les cas, l'extension .safetensors est garantie en sortie.
determine_lora_filename() {
  local url="$1"
  local override="${2:-}"
  local path_part="${url%%\?*}"
  local name=""

  if [[ -n "$override" ]]; then
    name="$override"
    [[ "$name" != *.safetensors ]] && name="${name}.safetensors"
    echo "$name"
    return 0
  fi

  if [[ "$path_part" == *.safetensors ]]; then
    basename "$path_part"
    return 0
  fi

  local cd_header
  local -a auth_args
  mapfile -t auth_args < <(civitai_auth_curl_args "$url")
  # Note : on utilise une requête GET à portée réduite (-r 0-0, un seul octet)
  # plutôt qu'un HEAD (-I). Le CDN de CivitAI ne renvoie souvent l'en-tête
  # Content-Disposition (contenant le vrai nom du fichier) que sur une vraie
  # réponse GET/206, pas sur un HEAD — d'où un nom de fichier tombant en repli
  # sur l'ID numérique de l'URL sans ce correctif.
  cd_header="$(curl -sL -r 0-0 "${auth_args[@]}" -D - -o /dev/null "$url" 2>/dev/null | tr -d '\r' | grep -i '^content-disposition:' | tail -n1)"
  if [[ -n "$cd_header" ]]; then
    name="$(echo "$cd_header" | sed -n 's/.*filename\*\{0,1\}=\(UTF-8..\)\{0,1\}"\{0,1\}\([^";]*\)"\{0,1\}.*/\2/p' | tail -n1)"
    if [[ -n "$name" ]] && command -v python3 >/dev/null 2>&1; then
      name="$(python3 -c "import urllib.parse,sys; print(urllib.parse.unquote(sys.argv[1]))" "$name" 2>/dev/null || echo "$name")"
    fi
  fi

  if [[ -z "$name" ]]; then
    name="$(basename "$path_part")"
    [[ -z "$name" || "$name" == "/" ]] && name="lora_download"
  fi

  [[ "$name" != *.safetensors ]] && name="${name}.safetensors"

  echo "$name"
}

# is_valid_safetensors_file <fichier>
# Vérifie que le fichier téléchargé n'est pas une page d'erreur HTML (page de
# connexion CivitAI, erreur Cloudflare, page 404 générique, etc.) déguisée en
# .safetensors. Un vrai fichier .safetensors commence par 8 octets (longueur
# de l'en-tête JSON, little-endian) suivis d'un objet JSON — jamais par des
# balises HTML. Contrôle volontairement simple (pas de dépendance externe
# obligatoire), complété par `file` si l'outil est disponible.
is_valid_safetensors_file() {
  local file="$1"
  local head_bytes
  head_bytes="$(head -c 512 -- "$file" 2>/dev/null | tr -d '\0')"

  if echo "$head_bytes" | grep -qi '<!doctype html\|<html[ >]\|<head[ >]\|<body[ >]'; then
    return 1
  fi

  if command -v file >/dev/null 2>&1; then
    local mime
    mime="$(file -b --mime-type -- "$file" 2>/dev/null || echo "")"
    if [[ "$mime" == "text/html" || "$mime" == "text/plain" ]]; then
      return 1
    fi
  fi

  return 0
}

# download_lora <url> <dest_file>
# Téléchargement avec reprise (-C -) et plusieurs tentatives, à l'image de
# download_hf_file() dans lib/download.sh mais sans dépendance à Hugging Face.
# Retourne :
#   0 -> succès (fichier présent et valide)
#   1 -> échec après épuisement des tentatives
#   2 -> échec d'authentification CivitAI (401/403), pas de nouvel essai
#   3 -> limite de débit atteinte (429), pas de nouvel essai
#   4 -> service distant temporairement indisponible (503), pas de nouvel essai
download_lora() {
  local url="$1" dest_file="$2"
  local attempt=1
  local -a auth_args
  mapfile -t auth_args < <(civitai_auth_curl_args "$url")

  while (( attempt <= DOWNLOAD_MAX_RETRIES )); do
    log_info "$(t loracli_attempt "$attempt" "$DOWNLOAD_MAX_RETRIES")"

    local http_code
    http_code="$(curl -sS -L -C - --retry 3 --retry-delay 5 "${auth_args[@]}" -o "$dest_file" -w '%{http_code}' "$url" 2>/dev/null || true)"

    if [[ "$http_code" == "401" || "$http_code" == "403" ]]; then
      rm -f -- "$dest_file" 2>/dev/null || true
      log_error "$(t loracli_auth_failed)"
      log_error "$(t loracli_check_apikey)"
      return 2
    fi

    if [[ "$http_code" == "429" ]]; then
      rm -f -- "$dest_file" 2>/dev/null || true
      if is_civitai_url "$url"; then
        log_error "$(t loracli_civitai_rate_limit)"
        log_error "$(t loracli_wait_retry)"
      else
        log_error "$(t loracli_rate_limit_generic)"
        log_error "$(t loracli_wait_retry)"
      fi
      return 3
    fi

    if [[ "$http_code" == "503" ]]; then
      rm -f -- "$dest_file" 2>/dev/null || true
      if is_civitai_url "$url"; then
        log_error "$(t loracli_civitai_unavailable)"
        log_error "$(t loracli_try_later)"
      else
        log_error "$(t loracli_remote_unavailable)"
        log_error "$(t loracli_try_later)"
      fi
      return 4
    fi

    if [[ "$http_code" =~ ^2 ]] && [[ -s "$dest_file" ]]; then
      if is_valid_safetensors_file "$dest_file"; then
        return 0
      fi
      log_warn "$(t loracli_invalid_file_retry)"
      rm -f -- "$dest_file"
    else
      log_warn "$(t loracli_dl_attempt_failed "$attempt" "$DOWNLOAD_MAX_RETRIES" "${http_code:-$(t gpu_cuda_unknown)}")"
      rm -f -- "$dest_file" 2>/dev/null || true
    fi

    ((attempt++))
    sleep 3
  done

  return 1
}

# format_size <octets>
# Formate une taille en octets en "N MB" ou "N.NN GB" (bascule en GB à partir
# de 1 Go), sans dépendance externe (awk seul, comme le reste du script).
format_size() {
  local bytes="${1:-0}"
  LC_ALL=C awk -v b="$bytes" 'BEGIN {
    gb = b / 1073741824
    mb = b / 1048576
    if (gb >= 1) {
      printf "%.2f GB", gb
    } else {
      mb_rounded = int(mb + 0.5)
      if (mb_rounded < 1) mb_rounded = 1
      printf "%d MB", mb_rounded
    }
  }'
}

# list_loras <dossier>
# Affiche tous les fichiers .safetensors présents dans <dossier>, numérotés,
# avec leur taille individuelle ainsi que le nombre total et la taille totale.
# Ne touche à rien, lecture seule.
list_loras() {
  local dir="$1"
  create_lora_folder "$dir"

  local files=()
  while IFS= read -r -d '' f; do
    files+=("$f")
  done < <(find "$dir" -maxdepth 1 -type f -name '*.safetensors' -print0 | sort -z)

  techo loracli_installed_header "$dir"

  if [[ ${#files[@]} -eq 0 ]]; then
    techo loracli_none_installed "$dir"
    return 0
  fi

  local total_bytes=0
  local i=1
  local f name bytes
  for f in "${files[@]}"; do
    name="$(basename "$f")"
    bytes="$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)"
    total_bytes=$(( total_bytes + bytes ))
    printf "%d. %-35s %10s\n" "$i" "$name" "$(format_size "$bytes")"
    ((i++))
  done

  echo "---------------------------------------"
  techo loracli_total_loras "${#files[@]}"
  techo loracli_total_size "$(format_size "$total_bytes")"
}

# remove_lora <dossier> <fichier>
# Supprime UNIQUEMENT le fichier indiqué dans <dossier>, après confirmation
# interactive. Ne supprime jamais le dossier lui-même, et rejette tout nom
# contenant un séparateur de chemin (protection contre une tentative de
# sortir du dossier).
remove_lora() {
  local dir="$1" requested="$2"

  if [[ "$requested" == */* ]]; then
    log_error "$(t loracli_invalid_filename "$requested")"
    exit 1
  fi

  local filename; filename="$(basename -- "$requested")"
  local target="${dir}/${filename}"

  if [[ ! -f "$target" ]]; then
    log_error "$(t loracli_lora_not_found "$filename" "$dir")"
    exit 1
  fi

  techo loracli_about_to_remove
  echo "  ${target}"
  read -r -p "$(t loracli_confirm_remove)" reply
  case "$reply" in
    y|Y|yes|Yes|YES|oui|Oui|OUI)
      rm -f -- "$target"
      log_ok "$(t loracli_removed "$filename")"
      ;;
    *)
      log_info "$(t loracli_remove_cancelled)"
      ;;
  esac
}

print_banner() {
  echo -e "${C_BOLD:-}${C_CYAN:-}"
  echo "  ┌────────────────────────────────────────────────────┐"
  printf '  │  %-50s│\n' "$(t loracli_banner_title)"
  echo "  └────────────────────────────────────────────────────┘"
  echo -e "${C_RESET:-}"
}

install_lora() {
  local url="$1" force="$2" filename_override="${3:-}" dir="$4"

  check_comfyui_installed
  create_lora_folder "$dir"

  local filename dest_file
  filename="$(determine_lora_filename "$url" "$filename_override")"
  dest_file="${dir}/${filename}"

  if [[ -n "$filename_override" ]]; then
    log_info "$(t loracli_filename_forced "$filename")"
  else
    log_info "$(t loracli_filename_detected "$filename")"
  fi

  if [[ -n "${CIVITAI_API_KEY:-}" ]] && is_civitai_url "$url"; then
    log_info "$(t loracli_civitai_auth)"
  fi

  if [[ -s "$dest_file" && "$force" != "true" ]]; then
    techo loracli_echo_already_installed
    log_ok "$(t loracli_already_installed "$filename" "$dest_file")"
    exit 0
  fi

  if [[ -s "$dest_file" && "$force" == "true" ]]; then
    log_warn "$(t loracli_force_redownload "$filename")"
    techo loracli_echo_download_again
  fi

  techo loracli_echo_downloading

  local dl_status=0
  download_lora "$url" "$dest_file" || dl_status=$?

  if [[ "$dl_status" -eq 2 || "$dl_status" -eq 3 || "$dl_status" -eq 4 ]]; then
    # Message déjà affiché par download_lora ; inutile de réessayer :
    # 2 = clé API invalide/expirée, 3 = limite de débit, 4 = service indisponible.
    exit 1
  elif [[ "$dl_status" -ne 0 ]]; then
    log_error "$(t loracli_download_failed "$DOWNLOAD_MAX_RETRIES")"
    rm -f "$dest_file"
    exit 1
  fi

  if [[ ! -s "$dest_file" ]]; then
    log_error "$(t loracli_empty_file "$dest_file")"
    rm -f "$dest_file"
    exit 1
  fi

  log_ok "$(t loracli_install_success)"
  techo loracli_echo_installed
  echo "  ${dest_file}"
}

main() {
  local action="install"
  local force="false"
  local personal="false"
  local manifest="false"
  local remove_target=""
  local filename_override=""
  local positional=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        exit 0
        ;;
      --list)
        action="list"
        shift
        ;;
      --remove)
        action="remove"
        shift
        if [[ $# -lt 1 ]]; then
          log_error "$(t loracli_remove_needs_arg)"
          usage
          exit 1
        fi
        remove_target="$1"
        shift
        ;;
      --force)
        force="true"
        shift
        ;;
      --personal)
        personal="true"
        shift
        ;;
      --manifest)
        manifest="true"
        shift
        ;;
      --filename)
        shift
        if [[ $# -lt 1 ]]; then
          log_error "$(t loracli_filename_needs_arg)"
          usage
          exit 1
        fi
        filename_override="$1"
        shift
        ;;
      -*)
        log_error "$(t loracli_unknown_option "$1")"
        usage
        exit 1
        ;;
      *)
        positional+=("$1")
        shift
        ;;
    esac
  done

  if [[ "$personal" == "true" && "$manifest" == "true" ]]; then
    log_error "$(t loracli_personal_manifest_exclusive)"
    exit 1
  fi

  local target_dir="$LORA_DIR"
  [[ "$personal" == "true" ]] && target_dir="$PERSONAL_LORA_DIR"
  [[ "$manifest" == "true" ]] && target_dir="$MANIFEST_LORA_DIR"

  case "$action" in
    list)
      list_loras "$target_dir"
      ;;
    remove)
      remove_lora "$target_dir" "$remove_target"
      ;;
    install)
      if [[ ${#positional[@]} -ne 1 ]]; then
        log_error "$(t loracli_one_url_expected)"
        usage
        exit 1
      fi

      local lora_url="${positional[0]}"
      if [[ ! "$lora_url" =~ ^https?:// ]]; then
        log_error "$(t loracli_invalid_url "$lora_url")"
        exit 1
      fi

      print_banner
      install_lora "$lora_url" "$force" "$filename_override" "$target_dir"
      if [[ "$personal" == "true" ]]; then
        log_info "$(t loracli_personal_saved_hint)"
      fi
      ;;
  esac
}

main "$@"
