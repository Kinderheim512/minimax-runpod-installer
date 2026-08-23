#!/usr/bin/env bash
# lib/verify.sh — vérifications avant lancement + résumé d'installation.
#
# CORRECTIF (problème 1) : la détection des modèles H3 se fait maintenant par
# recherche récursive (find) sous models/, au lieu de globs figés sur un
# sous-dossier précis. Les motifs ne portent que sur le préfixe stable du nom
# de fichier (minimax_h3_fl2va_, minimax_h3_ref2va_, qwen3vl*minimax_h3*,
# minimax_h3_video_vae_, minimax_h3_audio_vae_) : tout ce qui vient après
# (bf16, fp16, fp32, nvfp4_awq, int8_convrot, pruned_int8_convrot, etc.) est
# couvert par le caractère générique, sans avoir à toucher ce script si de
# nouvelles variantes sortent plus tard.

VERIFY_FAILED=0

_v_ok()   { log_ok "$1"; }
_v_fail() { log_error "$1"; VERIFY_FAILED=1; }
_v_warn() { log_warn "$1"; }

verify_installation() {
  log_step "$(t verify_step)"
  VERIFY_FAILED=0

  # --- GPU ---
  if require_cmd nvidia-smi && nvidia-smi >/dev/null 2>&1; then
    _v_ok "$(t verify_gpu_ok "${GPU_NAME:-$(t verify_gpu_detecting)}" "${GPU_VRAM_GB:-?}")"
  else
    _v_fail "$(t verify_gpu_fail)"
  fi

  # --- ComfyUI ---
  if [[ -f "${INSTALL_DIR}/main.py" ]]; then
    _v_ok "$(t verify_comfyui_ok "$INSTALL_DIR")"
  else
    _v_fail "$(t verify_comfyui_fail "$INSTALL_DIR")"
  fi

  # --- venv / torch ---
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    _v_ok "$(t verify_venv_ok "$VENV_DIR")"
    if "${VENV_DIR}/bin/python" -c "import torch" 2>/dev/null; then
      local tv cuda_ok
      tv="$("${VENV_DIR}/bin/python" -c 'import torch; print(torch.__version__)')"
      cuda_ok="$("${VENV_DIR}/bin/python" -c 'import torch; print(torch.cuda.is_available())')"
      if [[ "$cuda_ok" == "True" ]]; then
        _v_ok "$(t verify_torch_cuda_ok "$tv")"
      else
        _v_fail "$(t verify_torch_cuda_fail "$tv")"
      fi
    else
      _v_fail "$(t verify_torch_import_fail)"
    fi
  else
    _v_fail "$(t verify_venv_fail)"
  fi

  # --- comfy-kitchen (kernels accélérés FP8/NVFP4/INT8/ConvRot) ---
  # Dépendance pip standard de ComfyUI (épinglée dans son requirements.txt
  # amont, ex. "comfy-kitchen==0.2.31") — installée automatiquement par
  # install_comfyui_requirements() (lib/python.sh) en même temps que le
  # reste de requirements.txt, PAS un custom node séparé à cloner dans
  # custom_nodes/. Fournit les kernels natifs (convrot_w4a4,
  # int8_tensorwise, float8_e4m3fn...) dont dépendent les checkpoints
  # int8_convrot/int4_convrot (preset dasiwa_mmh3v12, palier standard
  # "balanced" fp8_scaled) pour tourner accéléré plutôt qu'en repli eager
  # PyTorch, nettement plus lent. Vérifié ici plutôt qu'ajouté quelque part
  # comme dépendance : un échec d'import connu en amont (comfy_kitchen mal
  # reconstruit après une mise à jour partielle, cf. Comfy-Org/ComfyUI#14766)
  # ne doit pas passer inaperçu silencieusement.
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    if "${VENV_DIR}/bin/python" -c "import comfy_kitchen" 2>/dev/null; then
      local ck_version
      ck_version="$("${VENV_DIR}/bin/python" -c 'import importlib.metadata as m; print(m.version("comfy-kitchen"))' 2>/dev/null)"
      _v_ok "$(t verify_kitchen_ok "${ck_version:-?}")"
    else
      _v_warn "$(t verify_kitchen_warn "${VENV_DIR}/bin/python")"
    fi
  fi

  # --- ComfyUI-Manager ---
  if [[ -d "${INSTALL_DIR}/custom_nodes/ComfyUI-Manager" ]]; then
    _v_ok "$(t verify_manager_ok)"
  else
    _v_warn "$(t verify_manager_warn)"
  fi

  # --- Modèles H3 : recherche récursive, peu importe le sous-dossier réel ---
  # Vérification consciente des workflows sélectionnés (resolve_h3_workflows,
  # même fonction que build_h3_model_manifest()/download_h3_models() —
  # seule source de vérité pour "quel workflow requiert quel modèle") :
  # FL2VA n'est exigé que si t2v/i2v est sélectionné, REF2VA que si r2v
  # l'est. Un modèle non requis par les workflows actifs n'est ni vérifié
  # ni signalé manquant.
  local base="${INSTALL_DIR}/models"
  local workflows; workflows="$(resolve_h3_workflows 2>/dev/null || echo "${H3_WORKFLOWS:-t2v,i2v,r2v}")"

  local _fl2va_files=() _ref2va_files=()
  if [[ -d "$base" ]]; then
    mapfile -t _fl2va_files < <(find "$base" -type f -iname "minimax_h3_fl2va_*.safetensors" 2>/dev/null)
    mapfile -t _ref2va_files < <(find "$base" -type f -iname "minimax_h3_ref2va_*.safetensors" 2>/dev/null)
  fi

  if _workflow_needs "$workflows" t2v i2v; then
    if (( ${#_fl2va_files[@]} > 0 )); then
      _v_ok "$(t verify_fl2va_ok)"
      for f in "${_fl2va_files[@]}"; do
        log_info "   - ${f#"$base"/} ($(du -h "$f" 2>/dev/null | cut -f1))"
      done
    else
      _v_fail "$(t verify_fl2va_fail)"
    fi
  fi

  if _workflow_needs "$workflows" r2v; then
    if (( ${#_ref2va_files[@]} > 0 )); then
      _v_ok "$(t verify_ref2va_ok)"
      for f in "${_ref2va_files[@]}"; do
        log_info "   - ${f#"$base"/} ($(du -h "$f" 2>/dev/null | cut -f1))"
      done
    else
      _v_fail "$(t verify_ref2va_fail)"
    fi
  fi

  local _text_encoder_files=()
  if [[ -d "$base" ]]; then
    mapfile -t _text_encoder_files < <(find "$base" -type f -iname "*qwen3vl*" -iname "*minimax_h3*" -iname "*.safetensors" 2>/dev/null)
  fi
  if (( ${#_text_encoder_files[@]} > 0 )); then
    _v_ok "$(t verify_text_encoder_ok)"
    for f in "${_text_encoder_files[@]}"; do
      log_info "   - ${f#"$base"/} ($(du -h "$f" 2>/dev/null | cut -f1))"
    done
  else
    _v_warn "$(t verify_text_encoder_warn)"
  fi

  local _video_vae="" _audio_vae=""
  if [[ -d "$base" ]]; then
    _video_vae="$(find "$base" -type f -iname "minimax_h3_video_vae_*.safetensors" 2>/dev/null | head -n1)"
    _audio_vae="$(find "$base" -type f -iname "minimax_h3_audio_vae_*.safetensors" 2>/dev/null | head -n1)"
  fi
  if [[ -n "$_video_vae" && -n "$_audio_vae" ]]; then
    _v_ok "$(t verify_vae_ok)"
  else
    [[ -z "$_video_vae" ]] && _v_warn "$(t verify_video_vae_warn)"
    [[ -z "$_audio_vae" ]] && _v_warn "$(t verify_audio_vae_warn)"
  fi

  # --- espace disque ---
  local free_gb; free_gb="$(free_disk_gb "$INSTALL_DIR")"
  _v_ok "$(t verify_disk_free "${free_gb:-$(t gpu_cuda_unknown)}" "$(dirname "$INSTALL_DIR")")"

  echo ""
  if (( VERIFY_FAILED == 0 )); then
    log_ok "$(t verify_all_ok)"
  else
    log_error "$(t verify_has_failures)"
  fi
  return "$VERIFY_FAILED"
}

print_summary() {
  echo ""
  echo -e "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════${C_RESET}"
  echo -e "${C_BOLD}$(t summary_title)${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════${C_RESET}"
  techo summary_comfyui_dir "$INSTALL_DIR"
  techo summary_gpu "${GPU_NAME:-?}" "${GPU_VRAM_GB:-?}"
  techo summary_tier "$(resolve_h3_tier 2>/dev/null || echo '?')"
  techo summary_workflows "$(resolve_h3_workflows 2>/dev/null || echo "${H3_WORKFLOWS:-?}")"
  local _presets_summary; _presets_summary="$(resolve_h3_presets 2>/dev/null || echo "")"
  [[ -n "$_presets_summary" ]] && techo summary_presets "$_presets_summary"
  techo summary_port "$COMFYUI_PORT"
  techo summary_logs "$LOG_DIR"
  echo -e "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════${C_RESET}"
  techo summary_next_step
  echo ""
}
