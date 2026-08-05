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

# Vérifie que l'utilisateur connecté a effectivement accès au dépôt gated
# de MiniMax H3 (licence acceptée). Retourne 0 si accès OK, 1 sinon.
hf_check_h3_access() {
  log_step "Vérification de l'accès au dépôt ${H3_HF_REPO}"

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  local http_code
  http_code="$(python - <<PYEOF
import os, urllib.request, urllib.error
token = os.environ.get("HF_TOKEN") or ""
if not token:
    try:
        from huggingface_hub import HfFolder
        token = HfFolder.get_token() or ""
    except Exception:
        pass
req = urllib.request.Request(
    "https://huggingface.co/api/models/${H3_HF_REPO}",
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
      log_ok "Accès au dépôt ${H3_HF_REPO} confirmé."
      return 0
      ;;
    401|403)
      log_error "Accès refusé (HTTP ${http_code}) au dépôt gated ${H3_HF_REPO}."
      log_error "Rendez-vous sur ${H3_LICENSE_URL} , connectez-vous, et acceptez la licence"
      log_error "'minimax-h3-community-license-agreement' avec le même compte que le token utilisé ici."
      return 1
      ;;
    *)
      log_warn "Impossible de vérifier l'accès (réponse HTTP ${http_code} ou réseau indisponible). On tentera le téléchargement directement."
      return 0
      ;;
  esac
}
