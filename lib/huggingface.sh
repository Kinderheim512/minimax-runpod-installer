#!/usr/bin/env bash
# lib/huggingface.sh — authentification Hugging Face officielle.
#
# On n'utilise jamais que les outils officiels (`hf auth login` /
# `huggingface-cli login`) et on ne contourne jamais une licence : si l'accès
# au dépôt MiniMax H3 n'a pas été accordé (licence non acceptée sur la page
# du modèle), on affiche l'URL à visiter et on arrête proprement l'étape de
# téléchargement des modèles — le reste de l'installation n'est pas impacté.

HF_CLI=""   # "hf" (nouveau) ou "huggingface-cli" (legacy) — détecté dynamiquement

detect_hf_cli() {
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  if require_cmd hf; then
    HF_CLI="hf"
  elif require_cmd huggingface-cli; then
    HF_CLI="huggingface-cli"
  else
    HF_CLI=""
  fi
}

hf_login() {
  log_step "Authentification Hugging Face"

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  detect_hf_cli
  if [[ -z "$HF_CLI" ]]; then
    log_error "Ni 'hf' ni 'huggingface-cli' ne sont disponibles dans le venv (installez requirements.txt d'abord)."
    deactivate
    exit 1
  fi

  # Déjà connecté ?
  if whoami_output="$("$HF_CLI" auth whoami 2>/dev/null || "$HF_CLI" whoami 2>/dev/null)"; then
    if [[ -n "$whoami_output" ]] && [[ "$whoami_output" != *"Not logged in"* ]]; then
      log_ok "Déjà connecté à Hugging Face (${whoami_output})."
      deactivate
      return 0
    fi
  fi

  if [[ -n "$HF_TOKEN" ]]; then
    log_info "Connexion avec le token fourni (variable HF_TOKEN)..."
    "$HF_CLI" auth login --token "$HF_TOKEN" >>"$LOG_FILE" 2>&1

  else
    log_info "Aucun token HF_TOKEN fourni : connexion interactive."
    echo -e "${C_YELLOW}Créez un token (lecture suffit) sur https://huggingface.co/settings/tokens si besoin.${C_RESET}"
    "$HF_CLI" auth login || "$HF_CLI" login
  fi
  deactivate

  log_ok "Authentification Hugging Face terminée."
}

# Vérifie que l'utilisateur connecté a effectivement accès à UN dépôt gated
# donné (licence acceptée). Retourne 0 si accès OK, 1 sinon.
# hf_check_repo_access <repo>
# Fonction générique, indépendante de tout dépôt en particulier : ne
# référence plus H3_HF_REPO/H3_LICENSE_URL en dur — c'est l'appelant qui
# fournit le dépôt à vérifier (voir hf_check_required_access() ci-dessous,
# seule fonction appelée par le reste du projet).
hf_check_repo_access() {
  local repo="$1"
  log_step "Vérification de l'accès au dépôt ${repo}"

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  local http_code
  http_code="$(python - "$repo" <<'PYEOF'
import os, sys, urllib.request, urllib.error
repo = sys.argv[1]
token = os.environ.get("HF_TOKEN") or ""
if not token:
    try:
        from huggingface_hub import HfFolder
        token = HfFolder.get_token() or ""
    except Exception:
        pass
req = urllib.request.Request(
    f"https://huggingface.co/api/models/{repo}",
    headers={"Authorization": f"Bearer {token}"} if token else {},
)
try:
    urllib.request.urlopen(req, timeout=15)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PYEOF
)"
  deactivate

  case "$http_code" in
    200)
      log_ok "Accès au dépôt ${repo} confirmé."
      return 0
      ;;
    401|403)
      log_error "Accès refusé (HTTP ${http_code}) au dépôt gated ${repo}."
      log_error "Rendez-vous sur https://huggingface.co/${repo} , connectez-vous, et acceptez la licence"
      log_error "de ce dépôt avec le même compte que le token utilisé ici."
      return 1
      ;;
    *)
      log_warn "Impossible de vérifier l'accès à ${repo} (réponse HTTP ${http_code} ou réseau indisponible). On tentera le téléchargement directement."
      return 0
      ;;
  esac
}

# hf_check_required_access <repo1> [repo2 ...]
# Vérifie l'accès à CHAQUE dépôt fourni (dédoublonnés par l'appelant — voir
# h3_required_repos() dans lib/models.sh) et échoue si au moins un est
# refusé. Point d'entrée générique appelé par download_missing_models() :
# le nombre et l'identité des dépôts réellement contactés dépendent
# uniquement du palier/des workflows sélectionnés, jamais figés en dur ici.
hf_check_required_access() {
  local repo ok=0
  for repo in "$@"; do
    hf_check_repo_access "$repo" || ok=1
  done
  return "$ok"
}
