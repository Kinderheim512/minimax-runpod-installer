#!/usr/bin/env bash
# lib/utils.sh — logging, gestion d'erreurs, helpers communs.
# Ce fichier est destiné à être "sourcé", jamais exécuté directement.

# shellcheck disable=SC2317  # `exit 0` semble inatteignable pour shellcheck car
# il suppose `return 0` toujours réussi ; c'est justement le repli voulu si ce
# fichier est exécuté directement (hors `source`), où `return` échouerait.
if [[ -n "${MINIMAX_UTILS_LOADED:-}" ]]; then return 0 2>/dev/null || exit 0; fi
MINIMAX_UTILS_LOADED=1

# ----------------------------------------------------------------------------
# Couleurs
# ----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
  C_BLUE=$'\033[0;34m'; C_CYAN=$'\033[0;36m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""; C_BOLD=""; C_RESET=""
fi

# ----------------------------------------------------------------------------
# Logging — chaque script appelant définit LOG_FILE avant de sourcer utils.sh
# (sinon on retombe sur logs/install.log)
# ----------------------------------------------------------------------------
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="${LOG_DIR:-${PROJECT_ROOT}/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/install.log}"

_ts() { date '+%Y-%m-%d %H:%M:%S'; }

log_info()  { echo -e "${C_BLUE}[INFO]${C_RESET}  $*" | tee -a "$LOG_FILE" >&2; }
log_ok()    { echo -e "${C_GREEN}[ OK ]${C_RESET}  $*" | tee -a "$LOG_FILE" >&2; }
log_warn()  { echo -e "${C_YELLOW}[WARN]${C_RESET}  $*" | tee -a "$LOG_FILE" >&2; }
log_error() { echo -e "${C_RED}[FAIL]${C_RESET}  $*" | tee -a "$LOG_FILE" >&2; }
log_step()  { echo -e "\n${C_BOLD}${C_CYAN}==> $*${C_RESET}" | tee -a "$LOG_FILE" >&2; }
log_raw()   { echo "$*" >> "$LOG_FILE"; }

# log_error_tail <label> [n_lines]
# Affiche directement dans le terminal les N dernières lignes de $LOG_FILE
# (défaut 25), encadrées et étiquetées. Utilisée après un échec dont la
# sortie complète (compilation, pip install...) a été redirigée uniquement
# vers le log — sans ça, l'utilisateur ne voit qu'un "consultez le log" et
# doit aller ouvrir le fichier lui-même pour connaître l'erreur réelle.
# Ne remplace pas log_warn/log_error (qui expliquent le contexte), s'utilise
# en complément juste après.
log_error_tail() {
  local label="$1"
  local n="${2:-25}"
  echo -e "${C_RED}${C_BOLD}----- Dernières lignes de sortie : ${label} -----${C_RESET}" >&2
  if [[ -f "$LOG_FILE" ]]; then
    while IFS= read -r _line; do echo "    ${_line}"; done < <(tail -n "$n" "$LOG_FILE") >&2
  else
    echo "    (log introuvable : ${LOG_FILE})" >&2
  fi
  echo -e "${C_RED}${C_BOLD}----- Fin de sortie (log complet : ${LOG_FILE}) -----${C_RESET}" >&2
}

# ----------------------------------------------------------------------------
# Gestion d'erreurs
# ----------------------------------------------------------------------------
# shellcheck disable=SC2317  # appelée uniquement via `trap '_on_error $LINENO' ERR`
# (enable_error_trap ci-dessous) : shellcheck ne trace pas les appels via trap,
# et marque donc tout le corps de la fonction comme "inaccessible".
_on_error() {
  local exit_code=$?
  local line_no=$1
  log_error "Échec ligne ${line_no} (code ${exit_code}) dans ${BASH_SOURCE[1]:-?}."
  log_error "Consultez ${LOG_FILE} pour le détail. Vous pouvez relancer install.sh : les étapes déjà validées seront sautées."
  exit "$exit_code"
}
enable_error_trap() {
  set -Eeuo pipefail
  trap '_on_error $LINENO' ERR
}

# ----------------------------------------------------------------------------
# Confirmation utilisateur
# ----------------------------------------------------------------------------
ASSUME_YES="${ASSUME_YES:-false}"
confirm() {
  local prompt="$1"
  if [[ "$ASSUME_YES" == "true" ]]; then return 0; fi
  local reply
  read -r -p "$(echo -e "${C_YELLOW}?${C_RESET} ${prompt} [o/N] ")" reply || true
  [[ "$reply" =~ ^([oOyY])([uUeE][iIsS])?$ ]]
}

# ----------------------------------------------------------------------------
# Divers helpers
# ----------------------------------------------------------------------------
require_cmd() {
  command -v "$1" >/dev/null 2>&1
}

retry() {
  # retry <n_tentatives> <commande...>
  local n="$1"; shift
  local i=1
  until "$@"; do
    if (( i >= n )); then
      log_error "Échec après ${n} tentatives : $*"
      return 1
    fi
    log_warn "Tentative ${i}/${n} échouée, nouvelle tentative dans 5s..."
    sleep 5
    ((i++))
  done
}

# wait_for_apt_lock [timeout_secs]
# Attend que le verrou dpkg (/var/lib/dpkg/lock-frontend) soit libre avant de
# lancer un apt-get. Sur RunPod, l'image de base lance souvent son propre
# apt-get/unattended-upgrades juste après le démarrage du pod ; sans cette
# attente, "apt-get install" échoue immédiatement avec "Could not get lock"
# et retry() (5 tentatives x 5s = ~20-25s) n'attend pas assez longtemps si
# l'autre process met plus de temps — l'installation du toolkit CUDA est
# alors abandonnée pour une raison qui n'a rien à voir avec le paquet
# lui-même. Timeout par défaut généreux (5 min) car ce process de fond peut
# être long sur un pod qui vient de démarrer.
wait_for_apt_lock() {
  local timeout="${1:-300}"
  local waited=0
  if ! require_cmd fuser; then
    return 0
  fi
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    if (( waited == 0 )); then
      log_info "Verrou dpkg occupé par un autre processus (apt-get de démarrage du pod probable) — attente jusqu'à ${timeout}s avant de lancer apt-get."
    fi
    if (( waited >= timeout )); then
      log_warn "Verrou dpkg toujours occupé après ${timeout}s — on tente quand même apt-get."
      return 0
    fi
    sleep 5
    ((waited+=5))
  done
  if (( waited > 0 )); then
    log_info "Verrou dpkg libéré après ${waited}s — poursuite."
  fi
}

human_gb() {
  # affiche un nombre d'octets en Go, 1 décimale
  awk -v b="$1" 'BEGIN{printf "%.1f Go", b/1000000000}'
}

# ----------------------------------------------------------------------------
# Progression des téléchargements
# ----------------------------------------------------------------------------
# DOWNLOAD_FILE_INDEX / DOWNLOAD_FILE_TOTAL sont positionnés par l'appelant
# (download_missing_models() dans lib/models.sh) avant de lancer une série de
# téléchargements — ce sont les seuls endroits qui savent combien de fichiers
# restent à récupérer pour la session en cours. Par défaut (0), announce_download()
# affiche juste le nom du fichier, sans compteur "i/N".
# Exportées ici et dans lib/models.sh : tous les lib/*.sh sont sourcés dans le
# même process (install.sh), donc l'export n'est pas nécessaire pour un
# sous-processus — mais c'est le signal que ShellCheck reconnaît pour une
# variable assignée dans un fichier et lue dans un autre (SC2034 : "Verify
# use (or export if used externally)"), ce qui évite un disable=SC2034 alors
# que l'usage est réel.
export DOWNLOAD_FILE_INDEX="${DOWNLOAD_FILE_INDEX:-0}"
export DOWNLOAD_FILE_TOTAL="${DOWNLOAD_FILE_TOTAL:-0}"

# announce_download <nom_de_fichier>
# Affiche un en-tête "[i/N] nom_de_fichier" avant de lancer un téléchargement
# et incrémente le compteur global. Le numéro/total de fichier est une
# information propre à l'installeur (aria2c/hf/curl ne peuvent pas la
# connaître, chacun ne voit qu'un fichier à la fois) — elle est affichée une
# fois ici, séparément de la progression native (%, débit, ETA...) que la
# commande de téléchargement elle-même affiche ensuite via run_with_progress().
announce_download() {
  local filename="$1"
  DOWNLOAD_FILE_INDEX=$(( DOWNLOAD_FILE_INDEX + 1 ))
  if [[ "$DOWNLOAD_FILE_TOTAL" -gt 0 ]]; then
    log_step "Téléchargement [${DOWNLOAD_FILE_INDEX}/${DOWNLOAD_FILE_TOTAL}] : ${filename}"
  else
    log_step "Téléchargement : ${filename}"
  fi
}

# run_with_progress -- <commande...>
# Exécute une commande en laissant sa progression NATIVE (barre aria2c,
# barres tqdm/rich de `hf download`, ou table curl) s'afficher en direct dans
# le terminal, tout en la consignant dans $LOG_FILE — aucun parsing de sortie
# ici, on se contente de dupliquer le flux avec `tee`.
#
# Un sous-processus qui écrit dans un pipe (`cmd | tee ...`) n'est plus vu
# comme un terminal par ce sous-processus : la plupart des outils de
# téléchargement retombent alors sur un rendu ligne par ligne au lieu d'une
# barre qui se redessine en place — toujours lisible (%, taille, débit, ETA
# restent tous présents), juste moins fluide visuellement. Quand `script`
# (util-linux, présent par défaut sur les images Ubuntu/RunPod) est
# disponible, on lui alloue un pseudo-terminal pour retrouver l'affichage en
# place identique à un lancement interactif direct ; sinon on retombe
# simplement sur `tee`.
#
# Retourne le code de sortie réel de la commande, jamais celui de `tee`.
run_with_progress() {
  [[ "${1:-}" == "--" ]] && shift
  local -a cmd=("$@")
  local rc

  if require_cmd script; then
    local quoted; quoted="$(printf '%q ' "${cmd[@]}")"
    # -q : silencieux sur les messages de `script` lui-même ; -e : renvoie le
    # code de sortie de la commande exécutée (pas celui de `script`) ; -f :
    # flush immédiat de chaque écriture, indispensable pour qu'un `\r` qui
    # redessine une barre de progression s'affiche en direct au lieu de
    # s'accumuler en mémoire tampon.
    script -qefc "$quoted" /dev/null 2>&1 | tee -a "$LOG_FILE"
    rc="${PIPESTATUS[0]}"
  else
    "${cmd[@]}" 2>&1 | tee -a "$LOG_FILE"
    rc="${PIPESTATUS[0]}"
  fi
  return "$rc"
}

free_disk_gb() {
  # espace libre en Go sur le point de montage du chemin donné
  local path="$1"
  df -PB1 "$path" 2>/dev/null | awk 'NR==2{printf "%.0f", $4/1000000000}'
}

# ----------------------------------------------------------------------------
# État d'installation (reprise après interruption)
# ----------------------------------------------------------------------------
state_file() { echo "${PROJECT_ROOT}/.minimax_installer_state"; }

step_done() {
  local step="$1"
  local f; f="$(state_file)"
  [[ -f "$f" ]] && grep -qxF "$step" "$f"
}

mark_step_done() {
  local step="$1"
  local f; f="$(state_file)"
  mkdir -p "$(dirname "$f")"
  touch "$f"
  grep -qxF "$step" "$f" 2>/dev/null || echo "$step" >> "$f"
}

run_step() {
  # run_step <nom_etape> <fonction> [force]
  local name="$1" fn="$2" force="${3:-false}"
  if [[ "$force" != "true" ]] && step_done "$name"; then
    log_ok "Étape '${name}' déjà réalisée, on passe. (--force pour refaire)"
    return 0
  fi
  "$fn"
  mark_step_done "$name"
}
