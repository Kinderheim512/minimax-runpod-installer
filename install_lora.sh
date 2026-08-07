#!/usr/bin/env bash
# install_lora.sh — gestionnaire de LoRA pour ComfyUI (MiniMax H3).
#
# Installe un LoRA depuis une URL directe dans models/loras/, liste les LoRA
# déjà installés, ou en supprime un. Sources officiellement supportées :
#   - Hugging Face  (ex: https://huggingface.co/.../resolve/main/mylora.safetensors)
#   - CivitAI       (https://civitai.com/... et https://civitai.red/...)
#   - toute URL directe pointant vers un fichier .safetensors
# Aucune authentification n'est requise pour les dépôts/LoRA publics sur ces
# trois sources.
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
#   ./install_lora.sh --force <URL>             réinstalle même si déjà présent
#   ./install_lora.sh --list                    liste les LoRA installés (avec taille)
#   ./install_lora.sh --remove <fichier>        supprime un LoRA (confirmation demandée)
#   ./install_lora.sh --help                    aide détaillée
#
# Exemples :
#   bash install_lora.sh https://huggingface.co/username/repo/resolve/main/mylora.safetensors
#   bash install_lora.sh https://civitai.com/api/download/models/123456
#   bash install_lora.sh https://civitai.red/api/download/models/3193337?fileId=3074134
#   bash install_lora.sh --force https://civitai.com/api/download/models/123456
#   bash install_lora.sh --list
#   bash install_lora.sh --remove anime_style.safetensors
#   CIVITAI_API_KEY=xxxxx bash install_lora.sh https://civitai.com/api/download/models/123456
#
# Script utilitaire additionnel : il ne modifie ni config.env, ni install.sh,
# ni le flux d'installation existant (lib/*.sh non touchés). Il n'a besoin
# d'aucun outil/SDK Hugging Face (pas de `hf`/`huggingface-cli`, pas de
# token) — uniquement curl (et python3, en option, pour décoder les URL),
# y compris pour les LoRA hébergés sur huggingface.co : un simple lien
# `resolve/main/...` est un fichier statique téléchargeable via curl, au
# même titre qu'un lien CivitAI ou tout autre lien .safetensors direct. La
# commande historique `bash install_lora.sh <URL>` continue de fonctionner
# exactement comme avant.

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
fi

INSTALL_DIR="${INSTALL_DIR:-/workspace/ComfyUI}"
LORA_DIR="${INSTALL_DIR}/models/loras"
DOWNLOAD_MAX_RETRIES="${DOWNLOAD_MAX_RETRIES:-5}"

usage() {
  cat <<EOF
Usage : $0 [OPTIONS] <URL>

Gestionnaire de LoRA pour ComfyUI — installe, liste ou supprime les LoRA
présents dans :
  ${LORA_DIR}

Sources supportées (aucune authentification requise pour du contenu public) :
  - Hugging Face : lien direct .../resolve/main/fichier.safetensors
  - CivitAI      : civitai.com et civitai.red
  - toute URL directe pointant vers un fichier .safetensors

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
      téléchargement est sauté ("LoRA already installed.").

      Exemples :
        $0 https://huggingface.co/username/repo/resolve/main/mylora.safetensors
        $0 https://civitai.com/api/download/models/123456
        $0 https://civitai.red/api/download/models/3193337?fileId=3074134

  $0 --force <URL>
      Force le retéléchargement même si le LoRA est déjà installé.

      Exemple :
        $0 --force https://civitai.com/api/download/models/123456

Consultation :
  $0 --list
      Affiche tous les LoRA installés, numérotés, avec la taille de chacun
      (MB, ou GB au-delà de 1 Go) ainsi que le nombre total et la taille
      totale.

      Exemple :
        $0 --list

Suppression :
  $0 --remove <fichier.safetensors>
      Supprime un LoRA précis (confirmation demandée). Ne supprime jamais
      le dossier models/loras/ lui-même, uniquement le fichier indiqué.

      Exemple :
        $0 --remove anime_style.safetensors

Autres :
  -h, --help
      Affiche cette aide.
EOF
}

check_comfyui_installed() {
  if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
    log_error "ComfyUI n'est pas installé dans ${INSTALL_DIR}. Lancez d'abord : bash install.sh"
    exit 1
  fi
}

create_lora_folder() {
  if [[ ! -d "$LORA_DIR" ]]; then
    log_info "Création du dossier ${LORA_DIR}"
    mkdir -p "$LORA_DIR"
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

# determine_lora_filename <url>
# Détermine le nom de fichier local le plus fiable possible :
#   1) URL se terminant directement par .safetensors -> on garde ce nom
#   2) CivitAI (civitai.com / civitai.red) -> le vrai nom est dans l'en-tête
#      Content-Disposition renvoyé après redirection, pas dans l'URL
#   3) Repli -> dernier segment de l'URL, sinon nom générique
# Dans tous les cas, l'extension .safetensors est garantie en sortie.
determine_lora_filename() {
  local url="$1"
  local path_part="${url%%\?*}"
  local name=""

  if [[ "$path_part" == *.safetensors ]]; then
    basename "$path_part"
    return 0
  fi

  local cd_header
  local -a auth_args
  mapfile -t auth_args < <(civitai_auth_curl_args "$url")
  cd_header="$(curl -sIL "${auth_args[@]}" "$url" 2>/dev/null | tr -d '\r' | grep -i '^content-disposition:' | tail -n1)"
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
    log_info "Tentative ${attempt}/${DOWNLOAD_MAX_RETRIES}..."

    local http_code
    http_code="$(curl -sS -L -C - --retry 3 --retry-delay 5 "${auth_args[@]}" -o "$dest_file" -w '%{http_code}' "$url" 2>/dev/null || true)"

    if [[ "$http_code" == "401" || "$http_code" == "403" ]]; then
      rm -f -- "$dest_file" 2>/dev/null || true
      log_error "Authentication failed with CivitAI."
      log_error "Please verify your CIVITAI_API_KEY environment variable."
      return 2
    fi

    if [[ "$http_code" == "429" ]]; then
      rm -f -- "$dest_file" 2>/dev/null || true
      if is_civitai_url "$url"; then
        log_error "CivitAI rate limit reached."
        log_error "Please wait a few minutes and try again."
      else
        log_error "Rate limit reached (HTTP 429)."
        log_error "Please wait a few minutes and try again."
      fi
      return 3
    fi

    if [[ "$http_code" == "503" ]]; then
      rm -f -- "$dest_file" 2>/dev/null || true
      if is_civitai_url "$url"; then
        log_error "CivitAI is temporarily unavailable."
        log_error "Please try again later."
      else
        log_error "Remote server temporarily unavailable (HTTP 503)."
        log_error "Please try again later."
      fi
      return 4
    fi

    if [[ "$http_code" =~ ^2 ]] && [[ -s "$dest_file" ]]; then
      if is_valid_safetensors_file "$dest_file"; then
        return 0
      fi
      log_warn "Fichier téléchargé invalide (page d'erreur ou HTML reçu au lieu d'un .safetensors), nouvel essai."
      rm -f -- "$dest_file"
    else
      log_warn "Échec du téléchargement (tentative ${attempt}/${DOWNLOAD_MAX_RETRIES}, code HTTP : ${http_code:-inconnu})."
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

# list_loras
# Affiche tous les fichiers .safetensors présents dans models/loras/, numérotés,
# avec leur taille individuelle ainsi que le nombre total et la taille totale.
# Ne touche à rien, lecture seule.
list_loras() {
  create_lora_folder

  local files=()
  while IFS= read -r -d '' f; do
    files+=("$f")
  done < <(find "$LORA_DIR" -maxdepth 1 -type f -name '*.safetensors' -print0 | sort -z)

  echo "Installed LoRAs"

  if [[ ${#files[@]} -eq 0 ]]; then
    echo "  (aucun LoRA installé dans ${LORA_DIR})"
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
  printf "Total LoRAs : %d\n" "${#files[@]}"
  printf "Total Size  : %s\n" "$(format_size "$total_bytes")"
}

# remove_lora <fichier>
# Supprime UNIQUEMENT le fichier indiqué dans models/loras/, après
# confirmation interactive. Ne supprime jamais le dossier lui-même, et
# rejette tout nom contenant un séparateur de chemin (protection contre une
# tentative de sortir de models/loras/).
remove_lora() {
  local requested="$1"

  if [[ "$requested" == */* ]]; then
    log_error "Nom de fichier invalide : ${requested} (indiquez uniquement le nom du fichier, sans chemin)."
    exit 1
  fi

  local filename; filename="$(basename -- "$requested")"
  local target="${LORA_DIR}/${filename}"

  if [[ ! -f "$target" ]]; then
    log_error "LoRA introuvable : ${filename} (recherché dans ${LORA_DIR})"
    exit 1
  fi

  echo "Vous êtes sur le point de supprimer :"
  echo "  ${target}"
  read -r -p "Confirmer la suppression ? [y/N] " reply
  case "$reply" in
    y|Y|yes|Yes|YES|oui|Oui|OUI)
      rm -f -- "$target"
      log_ok "LoRA supprimé : ${filename}"
      ;;
    *)
      log_info "Suppression annulée."
      ;;
  esac
}

print_banner() {
  echo -e "${C_BOLD:-}${C_CYAN:-}"
  echo "  ┌────────────────────────────────────────────────────┐"
  echo "  │  Gestionnaire de LoRA — MiniMax H3 / ComfyUI        │"
  echo "  └────────────────────────────────────────────────────┘"
  echo -e "${C_RESET:-}"
}

install_lora() {
  local url="$1" force="$2"

  check_comfyui_installed
  create_lora_folder

  local filename dest_file
  filename="$(determine_lora_filename "$url")"
  dest_file="${LORA_DIR}/${filename}"

  log_info "Nom de fichier détecté : ${filename}"

  if [[ -n "${CIVITAI_API_KEY:-}" ]] && is_civitai_url "$url"; then
    log_info "Using CivitAI API authentication."
  fi

  if [[ -s "$dest_file" && "$force" != "true" ]]; then
    echo "LoRA already installed."
    log_ok "${filename} est déjà installé (${dest_file}). Utilisez --force pour retélécharger."
    exit 0
  fi

  if [[ -s "$dest_file" && "$force" == "true" ]]; then
    log_warn "${filename} existe déjà, retéléchargement forcé (--force)."
    echo "Download again."
  fi

  echo "Downloading LoRA..."

  local dl_status=0
  download_lora "$url" "$dest_file" || dl_status=$?

  if [[ "$dl_status" -eq 2 || "$dl_status" -eq 3 || "$dl_status" -eq 4 ]]; then
    # Message déjà affiché par download_lora ; inutile de réessayer :
    # 2 = clé API invalide/expirée, 3 = limite de débit, 4 = service indisponible.
    exit 1
  elif [[ "$dl_status" -ne 0 ]]; then
    log_error "Échec du téléchargement du LoRA après ${DOWNLOAD_MAX_RETRIES} tentatives."
    rm -f "$dest_file"
    exit 1
  fi

  if [[ ! -s "$dest_file" ]]; then
    log_error "Le fichier téléchargé est vide : ${dest_file}"
    rm -f "$dest_file"
    exit 1
  fi

  log_ok "LoRA installé avec succès."
  echo "Installed:"
  echo "  ${dest_file}"
}

main() {
  local action="install"
  local force="false"
  local remove_target=""
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
          log_error "--remove attend le nom d'un fichier en argument."
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
      -*)
        log_error "Option inconnue : $1"
        usage
        exit 1
        ;;
      *)
        positional+=("$1")
        shift
        ;;
    esac
  done

  case "$action" in
    list)
      list_loras
      ;;
    remove)
      remove_lora "$remove_target"
      ;;
    install)
      if [[ ${#positional[@]} -ne 1 ]]; then
        log_error "Une seule URL est attendue en argument."
        usage
        exit 1
      fi

      local lora_url="${positional[0]}"
      if [[ ! "$lora_url" =~ ^https?:// ]]; then
        log_error "URL invalide : ${lora_url}"
        exit 1
      fi

      print_banner
      install_lora "$lora_url" "$force"
      ;;
  esac
}

main "$@"
