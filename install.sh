#!/usr/bin/env bash
# install.sh — automatic MiniMax H3 installer for RunPod + ComfyUI.
#
# Usage:
#   bash install.sh                       full install
#   bash install.sh --skip-models         installs everything except the H3 weights
#   bash install.sh --only-models         (re)downloads only the weights
#   bash install.sh --tier=light          forces a specific weight tier
#   bash install.sh --workflows=t2v,r2v   picks which bundled workflows to install
#   bash install.sh --preset=dasiwa_mmh3v12  downloads a preset's model set/
#                                          workflow (see H3_PRESET_NAMES,
#                                          config.env — some presets replace
#                                          the standard tier, others are
#                                          additive, see the comment on
#                                          H3_PRESET_REPLACES_STANDARD_TIER)
#   bash install.sh --yes                 non-interactive (answers "yes" everywhere)
#   bash install.sh --force               ignores already-completed steps (redoes everything)

set -Eeuo pipefail
# Conversion automatique des scripts Windows -> Linux
find "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" \
    -name "*.sh" \
    -exec sed -i 's/\r$//' {} \;
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/install.log"

SKIP_MODELS="false"
ONLY_MODELS="false"
FORCE="false"

for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS="true" ;;
    --only-models) ONLY_MODELS="true" ;;
    --force) FORCE="true" ;;
    --yes|-y) ASSUME_YES="true" ;;
    --tier=*) H3_TIER="${arg#*=}" ;;
    --workflows=*) H3_WORKFLOWS="${arg#*=}" ;;
    --preset=*) H3_PRESETS="${arg#*=}" ;;
    -h|--help)
      sed -n '2,/^[^#]/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    # Note: config.env/lib/utils.sh (and thus t()/INSTALLER_LANG) aren't
    # sourced yet at this point in argument parsing, so this one error
    # stays hardcoded in English rather than going through t().
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"
enable_error_trap
if ! command -v unzip >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y unzip
fi
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/system.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/gpu.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/python.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/comfyui.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/manager.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/nodes.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/huggingface.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/download.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/models.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/presets.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/lora_auto.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/workflows.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/optimization.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/verify.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/personal_storage.sh"

echo -e "${C_BOLD}${C_CYAN}"
echo "  ┌────────────────────────────────────────────────────┐"
printf '  │  %-50s│\n' "$(t install_banner_title)"
echo "  └────────────────────────────────────────────────────┘"
echo -e "${C_RESET}"

if [[ "$ONLY_MODELS" == "true" ]]; then
  if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
    log_error "$(t install_comfyui_missing "$INSTALL_DIR")"
    exit 1
  fi
  # detect_gpu (lib/gpu.sh) vérifie déjà nvidia-smi lui-même et quitte
  # proprement (avec un message d'erreur dédié) s'il est absent — un second
  # contrôle nvidia-smi ici serait du code mort, jamais atteint en cas
  # d'absence réelle du GPU puisque detect_gpu aurait déjà stoppé le script
  # juste avant.
  detect_gpu
  detect_system_ram
  run_step "python_venv" setup_python_venv "$FORCE"
  install_extra_requirements
  # Restauration du stockage perso (LoRAs/presets/outputs) — APRÈS
  # install_extra_requirements, pas juste après le venv : _personal_
  # storage_hf_ready() (lib/personal_storage.sh) a besoin que le CLI HF
  # (fourni par le paquet huggingface_hub, installé ici) soit réellement
  # présent dans le venv, pas seulement que le venv existe — un venv tout
  # juste créé est vide. Dans cette branche (--only-models),
  # install_comfyui_requirements n'est jamais appelée, donc
  # install_extra_requirements est la SEULE source de ce CLI : ne jamais
  # placer cet appel avant. Sur un pod tout neuf, un mauvais placement fait
  # échouer silencieusement le pull à CHAQUE fois — et comme update.sh ne
  # fait que pousser (jamais tirer), le coffre perso ne serait alors jamais
  # restauré automatiquement. No-op silencieux si PERSONAL_STORAGE_HF_REPO/
  # PERSONAL_LORAS_GITHUB_RELEASE_URL sont vides.
  sync_personal_storage_pull
  create_model_folders
  run_step "hf_login" hf_login "$FORCE"
  H3_ACTIVE_PRESETS="$(resolve_h3_presets)"
  if [[ "$(preset_replaces_standard_tier "$H3_ACTIVE_PRESETS")" == "true" ]]; then
    log_info "$(t install_preset_replaces_tier "$H3_ACTIVE_PRESETS")"
  else
    if download_h3_models; then mark_step_done "h3_models"; fi
  fi
  install_turbo_node
  install_turbo_lora
  if [[ -n "$H3_ACTIVE_PRESETS" ]]; then
    install_preset_nodes "$H3_ACTIVE_PRESETS"
    install_preset_pip_packages "$H3_ACTIVE_PRESETS"
    if download_preset_models "$H3_ACTIVE_PRESETS"; then
      install_preset_symlinks "$H3_ACTIVE_PRESETS"
      install_preset_workflows "$H3_ACTIVE_PRESETS"
    else
      log_warn "$(t install_preset_download_incomplete "$H3_ACTIVE_PRESETS" "$H3_ACTIVE_PRESETS")"
    fi
  fi
  print_summary
  exit 0
fi

run_step "system_packages"    install_system_packages    "$FORCE"
detect_gpu
detect_system_ram
if ! command -v git-lfs >/dev/null 2>&1; then
    apt-get install -y git-lfs
    git lfs install
fi
run_step "comfyui_cloned"     clone_or_update_comfyui     "$FORCE"
run_step "python_venv"        setup_python_venv          "$FORCE"
run_step "comfyui_requirements" install_comfyui_requirements "$FORCE"
install_extra_requirements
# Restauration du stockage perso (LoRAs/presets/outputs) — APRÈS
# install_extra_requirements (voir le commentaire équivalent, plus détaillé,
# dans la branche ONLY_MODELS ci-dessus) : le CLI HF doit être réellement
# installé, pas seulement le venv créé.
sync_personal_storage_pull
# Pas de run_step ici volontairement : l'idempotence d'install_sageattention
# vient de son propre check `import sageattention` (cf. lib/python.sh), pas
# d'un state-file. Un run_step marquerait l'étape "faite" même en cas
# d'échec réseau/compilation transitoire, empêchant toute nouvelle tentative
# au prochain lancement sans passer par --force (qui refait tout le reste).
install_sageattention
run_step "manager_installed"  install_or_update_manager   "$FORCE"
run_step "optional_nodes"     install_optional_nodes      "$FORCE"
run_step "model_folders"      create_model_folders        "$FORCE"

# --- Presets : résolu une fois ici (avant la décision "faut-il encore
#     télécharger le palier standard H3_TIER ?" ci-dessous), réutilisé plus
#     bas pour l'installation des presets eux-mêmes. -----------------------
H3_ACTIVE_PRESETS="$(resolve_h3_presets)"

if [[ "$SKIP_MODELS" == "false" ]]; then
  run_step "hf_login" hf_login "$FORCE"
  if [[ "$(preset_replaces_standard_tier "$H3_ACTIVE_PRESETS")" == "true" ]]; then
    log_info "$(t install_preset_replaces_tier "$H3_ACTIVE_PRESETS")"
  elif ! step_done "h3_models" || [[ "$FORCE" == "true" ]]; then
    # Estimation dynamique de l'espace requis : mêmes fonctions que celles
    # utilisées à l'intérieur de download_h3_models() (lib/models.sh), donc
    # une seule source de vérité pour "combien d'espace il faut" — plus de
    # valeur figée (l'ancien seuil de 140 Go ignorait le palier H3_TIER et
    # les workflows H3_WORKFLOWS sélectionnés).
    H3_PREFLIGHT_TIER="$(resolve_h3_tier)"
    H3_PREFLIGHT_WORKFLOWS="$(resolve_h3_workflows)"
    build_h3_model_manifest "$H3_PREFLIGHT_TIER"
    collect_missing_models "${INSTALL_DIR}/models"
    H3_PREFLIGHT_EST_GB="$(estimate_missing_download_size_gb "$H3_PREFLIGHT_TIER" "$H3_PREFLIGHT_WORKFLOWS")"
    H3_PREFLIGHT_FREE_GB="$(free_disk_gb "$INSTALL_DIR")"

    if [[ -n "$H3_PREFLIGHT_FREE_GB" ]] && awk -v f="$H3_PREFLIGHT_FREE_GB" -v e="$H3_PREFLIGHT_EST_GB" 'BEGIN{exit !(f < e)}'; then
      log_error "$(t install_disk_insufficient_title)"
      log_error "$(t install_disk_insufficient_detail "$H3_PREFLIGHT_TIER" "$H3_PREFLIGHT_WORKFLOWS" "$H3_PREFLIGHT_EST_GB" "$H3_PREFLIGHT_FREE_GB")"
      exit 1
    fi

    if download_h3_models; then
      mark_step_done "h3_models"
    else
      log_warn "$(t install_models_incomplete)"
    fi
  else
    log_ok "$(t install_models_already_done)"
  fi
  install_turbo_node
  install_turbo_lora
else
  log_info "$(t install_skip_models_notice)"
fi

# --- Presets (additif, indépendant de --skip-models : un --preset= explicite
#     reste honoré même si les poids standard sont sautés — H3_ACTIVE_PRESETS
#     déjà résolu plus haut) ---------------------------------------------
if [[ -n "$H3_ACTIVE_PRESETS" ]]; then
  install_preset_nodes "$H3_ACTIVE_PRESETS"
  install_preset_pip_packages "$H3_ACTIVE_PRESETS"
  if download_preset_models "$H3_ACTIVE_PRESETS"; then
    install_preset_symlinks "$H3_ACTIVE_PRESETS"
    install_preset_workflows "$H3_ACTIVE_PRESETS"
  else
    log_warn "$(t install_preset_download_incomplete "$H3_ACTIVE_PRESETS" "$H3_ACTIVE_PRESETS")"
  fi
fi

# Sauté quand le preset actif remplace le palier standard (même garde que
# pour download_h3_models() ci-dessus) : les workflows officiels t2v/i2v/r2v
# référencent les poids du palier standard H3_TIER, jamais téléchargés dans
# ce cas — les copier créerait des workflows "fantômes" dans ComfyUI,
# pointant vers des fichiers absents. Le workflow propre au preset a déjà
# été installé plus haut par install_preset_workflows(), indépendamment de
# ce garde-fou.
if [[ "$(preset_replaces_standard_tier "$H3_ACTIVE_PRESETS")" == "true" ]]; then
  log_info "$(t install_preset_workflows_skipped "$H3_ACTIVE_PRESETS")"
else
  run_step "workflows" install_workflows "$FORCE"
fi
run_step "optimization" compute_optimization_flags "$FORCE"
verify_installation || true
print_summary

log_ok "$(t install_done)"
