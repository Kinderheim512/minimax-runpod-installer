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
  log_step "$(t hf_login_step)"

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  detect_hf_cli
  if [[ -z "$HF_CLI" ]]; then
    log_error "$(t hf_no_cli)"
    deactivate
    exit 1
  fi

  # Déjà connecté ?
  if whoami_output="$("$HF_CLI" auth whoami 2>/dev/null || "$HF_CLI" whoami 2>/dev/null)"; then
    if [[ -n "$whoami_output" ]] && [[ "$whoami_output" != *"Not logged in"* ]]; then
      log_ok "$(t hf_already_logged_in "$whoami_output")"
      deactivate
      return 0
    fi
  fi

  if [[ -n "$HF_TOKEN" ]]; then
    log_info "$(t hf_login_with_token)"
    "$HF_CLI" auth login --token "$HF_TOKEN" >>"$LOG_FILE" 2>&1

  else
    log_info "$(t hf_no_token_interactive)"
    echo -e "${C_YELLOW}$(t hf_token_hint)${C_RESET}"
    "$HF_CLI" auth login || "$HF_CLI" login
  fi
  deactivate

  log_ok "$(t hf_login_done)"
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
  log_step "$(t hf_access_check_step "$repo")"

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
      log_ok "$(t hf_access_confirmed "$repo")"
      return 0
      ;;
    401|403)
      log_error "$(t hf_access_denied "$http_code" "$repo")"
      log_error "$(t hf_access_denied_fix1 "$repo")"
      log_error "$(t hf_access_denied_fix2)"
      return 1
      ;;
    *)
      log_warn "$(t hf_access_check_failed "$repo" "$http_code")"
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
