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
  log_step "Vérification de l'installation"
  VERIFY_FAILED=0

  # --- GPU ---
  if require_cmd nvidia-smi && nvidia-smi >/dev/null 2>&1; then
    _v_ok "GPU accessible : ${GPU_NAME:-détection en cours} (${GPU_VRAM_GB:-?} Go)"
  else
    _v_fail "GPU non accessible via nvidia-smi."
  fi

  # --- ComfyUI ---
  if [[ -f "${INSTALL_DIR}/main.py" ]]; then
    _v_ok "ComfyUI présent dans ${INSTALL_DIR}"
  else
    _v_fail "main.py introuvable dans ${INSTALL_DIR} — ComfyUI n'est pas installé."
  fi

  # --- venv / torch ---
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    _v_ok "Environnement virtuel présent (${VENV_DIR})"
    if "${VENV_DIR}/bin/python" -c "import torch" 2>/dev/null; then
      local tv cuda_ok
      tv="$("${VENV_DIR}/bin/python" -c 'import torch; print(torch.__version__)')"
      cuda_ok="$("${VENV_DIR}/bin/python" -c 'import torch; print(torch.cuda.is_available())')"
      if [[ "$cuda_ok" == "True" ]]; then
        _v_ok "PyTorch ${tv} — CUDA disponible."
      else
        _v_fail "PyTorch ${tv} installé mais CUDA indisponible pour torch."
      fi
    else
      _v_fail "PyTorch non importable dans le venv."
    fi
  else
    _v_fail "Environnement virtuel absent."
  fi

  # --- ComfyUI-Manager ---
  if [[ -d "${INSTALL_DIR}/custom_nodes/ComfyUI-Manager" ]]; then
    _v_ok "ComfyUI-Manager installé."
  else
    _v_warn "ComfyUI-Manager absent (facultatif mais recommandé)."
  fi

  # --- Modèles H3 : recherche récursive, peu importe le sous-dossier réel ---
  local base="${INSTALL_DIR}/models"

  local _diffusion_files=()
  if [[ -d "$base" ]]; then
    mapfile -t _diffusion_files < <(find "$base" -type f \
      \( -iname "minimax_h3_fl2va_*.safetensors" -o -iname "minimax_h3_ref2va_*.safetensors" \) \
      2>/dev/null)
  fi
  if (( ${#_diffusion_files[@]} > 0 )); then
    _v_ok "Au moins un modèle de diffusion MiniMax H3 présent (${#_diffusion_files[@]})."
    for f in "${_diffusion_files[@]}"; do
      log_info "   - ${f#"$base"/} ($(du -h "$f" 2>/dev/null | cut -f1))"
    done
  else
    _v_warn "Aucun modèle de diffusion MiniMax H3 trouvé — lancez : bash install.sh --only-models"
  fi

  local _text_encoder_files=()
  if [[ -d "$base" ]]; then
    mapfile -t _text_encoder_files < <(find "$base" -type f -iname "*qwen3vl*" -iname "*minimax_h3*" -iname "*.safetensors" 2>/dev/null)
  fi
  if (( ${#_text_encoder_files[@]} > 0 )); then
    _v_ok "Encodeur de texte MiniMax H3 présent."
    for f in "${_text_encoder_files[@]}"; do
      log_info "   - ${f#"$base"/} ($(du -h "$f" 2>/dev/null | cut -f1))"
    done
  else
    _v_warn "Encodeur de texte MiniMax H3 manquant."
  fi

  local _video_vae="" _audio_vae=""
  if [[ -d "$base" ]]; then
    _video_vae="$(find "$base" -type f -iname "minimax_h3_video_vae_*.safetensors" 2>/dev/null | head -n1)"
    _audio_vae="$(find "$base" -type f -iname "minimax_h3_audio_vae_*.safetensors" 2>/dev/null | head -n1)"
  fi
  if [[ -n "$_video_vae" && -n "$_audio_vae" ]]; then
    _v_ok "VAE vidéo + audio MiniMax H3 présents."
  else
    [[ -z "$_video_vae" ]] && _v_warn "VAE vidéo MiniMax H3 manquant."
    [[ -z "$_audio_vae" ]] && _v_warn "VAE audio MiniMax H3 manquant."
  fi

  # --- espace disque ---
  local free_gb; free_gb="$(free_disk_gb "$INSTALL_DIR")"
  _v_ok "Espace disque libre : ${free_gb:-inconnu} Go sur $(dirname "$INSTALL_DIR")"

  echo ""
  if (( VERIFY_FAILED == 0 )); then
    log_ok "Vérification terminée : tout est en ordre pour un lancement."
  else
    log_error "Vérification terminée avec des échecs critiques — voir ci-dessus avant de lancer ComfyUI."
  fi
  return "$VERIFY_FAILED"
}

print_summary() {
  echo ""
  echo -e "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════${C_RESET}"
  echo -e "${C_BOLD} Résumé de l'installation MiniMax H3 / ComfyUI ${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════${C_RESET}"
  echo "  Répertoire ComfyUI : ${INSTALL_DIR}"
  echo "  GPU                : ${GPU_NAME:-?} (${GPU_VRAM_GB:-?} Go VRAM)"
  echo "  Palier de poids H3 : $(resolve_h3_tier 2>/dev/null || echo '?')"
  echo "  Workflows préparés : ${H3_WORKFLOWS}"
  echo "  Port d'écoute      : ${COMFYUI_PORT}"
  echo "  Logs                : ${LOG_DIR}"
  echo -e "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════${C_RESET}"
  echo "  Prochaine étape : ./launch.sh"
  echo ""
}
