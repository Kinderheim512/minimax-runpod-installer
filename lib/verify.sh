#!/usr/bin/env bash
# lib/verify.sh — vérifications avant lancement + résumé d'installation.

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

  # --- Modèles H3 ---
  local base="${INSTALL_DIR}/models"
  local tier; tier="$(resolve_h3_tier 2>/dev/null || echo "?")"
  local workflows="$H3_WORKFLOWS"; [[ "$workflows" == "all" ]] && workflows="t2v,i2v,r2v"
  local any_diffusion=0

  for f in "${base}/diffusion_models"/minimax_h3_*.safetensors; do
    [[ -f "$f" ]] && { any_diffusion=1; break; }
  done
  if (( any_diffusion )); then
    _v_ok "Au moins un modèle de diffusion MiniMax H3 présent."
    for f in "${base}/diffusion_models"/minimax_h3_*.safetensors; do
      [[ -f "$f" ]] && log_info "   - $(basename "$f") ($(du -h "$f" 2>/dev/null | cut -f1))"
    done
  else
    _v_warn "Aucun modèle de diffusion MiniMax H3 trouvé — lancez : bash install.sh --only-models"
  fi

  if compgen -G "${base}/text_encoders/qwen3vl_32b_minimax_h3_*.safetensors" > /dev/null; then
    _v_ok "Encodeur de texte MiniMax H3 présent."
  else
    _v_warn "Encodeur de texte MiniMax H3 manquant."
  fi

  if [[ -f "${base}/vae/minimax_h3_video_vae_fp16.safetensors" && -f "${base}/vae/minimax_h3_audio_vae_fp32.safetensors" ]]; then
    _v_ok "VAE vidéo + audio MiniMax H3 présents."
  else
    _v_warn "VAE MiniMax H3 (vidéo et/ou audio) manquant(s)."
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
