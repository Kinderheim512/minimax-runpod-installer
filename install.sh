#!/usr/bin/env bash
# install.sh — installateur automatique MiniMax H3 pour RunPod + ComfyUI.
#
# Usage :
#   bash install.sh                       installation complète
#   bash install.sh --skip-models         installe tout sauf les poids H3
#   bash install.sh --only-models         (ré)télécharge uniquement les poids
#   bash install.sh --tier=light          force un palier de poids
#   bash install.sh --workflows=t2v,r2v   choisit les workflows préparés
#   bash install.sh --preset=aistudynow   télécharge en plus les modèles/le
#                                          workflow d'un preset (additif —
#                                          n'affecte jamais --tier/--workflows)
#   bash install.sh --yes                 non interactif (répond "oui" partout)
#   bash install.sh --force               ignore l'état déjà validé (réexécute tout)

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
    *) echo "Option inconnue : $arg" >&2; exit 1 ;;
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

echo -e "${C_BOLD}${C_CYAN}"
echo "  ┌────────────────────────────────────────────────────┐"
echo "  │  Installateur MiniMax H3 pour RunPod + ComfyUI      │"
echo "  └────────────────────────────────────────────────────┘"
echo -e "${C_RESET}"

if [[ "$ONLY_MODELS" == "true" ]]; then
  if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
    log_error "ComfyUI n'est pas installé dans ${INSTALL_DIR}. Lancez d'abord : bash install.sh"
    exit 1
  fi
  detect_gpu
  detect_system_ram
nvidia-smi || {
    log_error "GPU NVIDIA non détecté."
    exit 1
}
  run_step "python_venv" setup_python_venv "$FORCE"
  install_extra_requirements
  create_model_folders
  run_step "hf_login" hf_login "$FORCE"
  if download_h3_models; then mark_step_done "h3_models"; fi
  install_turbo_node
  install_turbo_lora
  H3_ACTIVE_PRESETS="$(resolve_h3_presets)"
  if [[ -n "$H3_ACTIVE_PRESETS" ]]; then
    install_preset_nodes "$H3_ACTIVE_PRESETS"
    if download_preset_models "$H3_ACTIVE_PRESETS"; then
      install_preset_symlinks "$H3_ACTIVE_PRESETS"
      install_preset_workflows "$H3_ACTIVE_PRESETS"
    else
      log_warn "Téléchargement du/des preset(s) '${H3_ACTIVE_PRESETS}' incomplet — relancez plus tard : bash install.sh --only-models --preset=${H3_ACTIVE_PRESETS}"
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
# Pas de run_step ici volontairement : l'idempotence d'install_sageattention
# vient de son propre check `import sageattention` (cf. lib/python.sh), pas
# d'un state-file. Un run_step marquerait l'étape "faite" même en cas
# d'échec réseau/compilation transitoire, empêchant toute nouvelle tentative
# au prochain lancement sans passer par --force (qui refait tout le reste).
install_sageattention
run_step "manager_installed"  install_or_update_manager   "$FORCE"
run_step "optional_nodes"     install_optional_nodes      "$FORCE"
run_step "model_folders"      create_model_folders        "$FORCE"

if [[ "$SKIP_MODELS" == "false" ]]; then
  run_step "hf_login" hf_login "$FORCE"
  if ! step_done "h3_models" || [[ "$FORCE" == "true" ]]; then
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
      log_error "Pas assez d'espace disque."
      log_error "Requis (estimation, palier ${H3_PREFLIGHT_TIER}, workflows ${H3_PREFLIGHT_WORKFLOWS}) : ~${H3_PREFLIGHT_EST_GB} Go — libre : ${H3_PREFLIGHT_FREE_GB} Go."
      exit 1
    fi

    if download_h3_models; then
      mark_step_done "h3_models"
    else
      log_warn "Téléchargement des modèles incomplet — relancez plus tard : bash install.sh --only-models"
    fi
  else
    log_ok "Modèles H3 déjà téléchargés, étape sautée."
  fi
  install_turbo_node
  install_turbo_lora
else
  log_info "--skip-models : téléchargement des poids H3 sauté (à faire plus tard via menu.sh ou --only-models)."
fi

# --- Presets (additif, indépendant de --skip-models : un --preset= explicite
#     reste honoré même si les poids standard sont sautés) -------------------
H3_ACTIVE_PRESETS="$(resolve_h3_presets)"
if [[ -n "$H3_ACTIVE_PRESETS" ]]; then
  install_preset_nodes "$H3_ACTIVE_PRESETS"
  if download_preset_models "$H3_ACTIVE_PRESETS"; then
    install_preset_symlinks "$H3_ACTIVE_PRESETS"
    install_preset_workflows "$H3_ACTIVE_PRESETS"
  else
    log_warn "Téléchargement du/des preset(s) '${H3_ACTIVE_PRESETS}' incomplet — relancez plus tard : bash install.sh --only-models --preset=${H3_ACTIVE_PRESETS}"
  fi
fi

run_step "workflows" install_workflows "$FORCE"
run_step "optimization" compute_optimization_flags "$FORCE"
verify_installation || true
print_summary

log_ok "Installation terminée. Lancez ComfyUI avec : ./launch.sh"
