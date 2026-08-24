#!/usr/bin/env bash
# lib/optimization.sh — calcule les arguments de lancement ComfyUI et les
# variables d'environnement adaptés au GPU détecté, et les écrit dans un
# fichier que launch.sh se contente de sourcer. Toutes les options utilisées
# ici sont des flags officiels documentés de ComfyUI/main.py (--lowvram,
# --highvram, --reserve-vram, --use-pytorch-cross-attention, --fast, etc.).

LAUNCH_FLAGS_FILE_NAME=".minimax_launch_flags"

compute_optimization_flags() {
  log_step "$(t opt_step)"

  local flags=()
  local env_vars=()

  env_vars+=("PYTORCH_CUDA_ALLOC_CONF=expandable_segments:False")
  env_vars+=("HF_XET_HIGH_PERFORMANCE=1")

  # --- gestion mémoire selon la VRAM -------------------------------------
  # ComfyUI utilise désormais DynamicVRAM par défaut (NVIDIA + PyTorch 2.8+
  # + non-WSL — cf. blog.comfy.org "Dynamic VRAM in ComfyUI: Saving Local
  # Models from RAMmageddon", et discussion Comfy-Org/ComfyUI #12699). Ce
  # système surveille la pression mémoire réelle PENDANT l'exécution et
  # déplace les poids entre GPU/RAM/disque à la demande, au lieu de
  # l'ancienne estimation faite une fois au chargement — plus d'OOM causés
  # par un offloading insuffisant, chargement de modèle quasi instantané.
  # Le venv de ce projet installe toujours PyTorch bien au-dessus de 2.8
  # (voir PYTORCH_BUILD_TABLE, lib/python.sh) sur des conteneurs Linux
  # jamais WSL : les conditions sont donc réunies sur toute installation
  # standard de ce projet.
  #
  # Point critique (ancien historique de ce fichier, corrigé ici) :
  # --highvram fait partie de la liste OFFICIELLE des flags qui DÉSACTIVENT
  # DynamicVRAM (avec --disable-dynamic-vram, --gpu-only, --novram, --cpu).
  # Le forcer revient donc à revenir à l'ANCIEN système, moins performant et
  # plus sujet aux OOM — l'exact opposé de l'effet recherché. --reserve-vram
  # n'est pas dans cette liste, mais devient largement redondant : la
  # recommandation communautaire actuelle (cf. guide InstaSD "ComfyUI VRAM
  # Guide: Run MiniMax H3 on 8-16 GB Cards") est qu'à partir de 24 Go de
  # VRAM, aucun flag de gestion mémoire n'est nécessaire — DynamicVRAM gère
  # déjà tout seul, mieux que ces réglages manuels.
  #   - true  : force --highvram — DÉCONSEILLÉ désormais (désactive
  #             DynamicVRAM), gardé uniquement pour compatibilité/debug si
  #             vous devez vraiment tout garder en VRAM sans offloading.
  #   - false : ne force jamais --highvram (recommandé).
  #   - auto  (défaut) : aucun flag de gestion VRAM sur les GPU >= 24 Go
  #             (laisse DynamicVRAM opérer) ; conserve un repli
  #             --lowvram --reserve-vram 1 en dessous de 24 Go, faute de
  #             retour d'expérience sur ce segment avec DynamicVRAM.
  local highvram_mode="${COMFY_HIGHVRAM:-auto}"

  case "$highvram_mode" in
    true)
      flags+=("--highvram")
      log_info "$(t opt_highvram_forced)"
      ;;
    false)
      if (( GPU_VRAM_GB < 24 )); then
        flags+=("--lowvram" "--reserve-vram" "1")
        log_info "$(t opt_highvram_false_lt24)"
      else
        log_info "$(t opt_dynamicvram_trusted "$GPU_VRAM_GB")"
      fi
      ;;
    auto|*)
      if (( GPU_VRAM_GB < 24 )); then
        flags+=("--lowvram" "--reserve-vram" "1")
        log_info "$(t opt_highvram_auto_lt24)"
      else
        log_info "$(t opt_dynamicvram_trusted "$GPU_VRAM_GB")"
      fi
      ;;
  esac

  # --- pinned memory (Dynamic VRAM) selon la RAM réellement allouée -----
  # Voir le commentaire de COMFY_PINNED_MEMORY dans config.env pour le
  # contexte complet (SIGKILL cgroup silencieux sur pods RAM-limités).
  # Garde défensive : compute_optimization_flags() est appelée depuis
  # plusieurs scripts (install.sh, update.sh) qui appellent tous déjà
  # detect_gpu() avant — même convention ici, mais si detect_system_ram()
  # n'a exceptionnellement pas encore tourné (SYSTEM_RAM_LIMIT_GB à 0), on
  # la déclenche nous-mêmes plutôt que de calculer sur une valeur absente.
  if (( SYSTEM_RAM_LIMIT_GB == 0 )); then
    detect_system_ram
  fi

  local pinned_mode="${COMFY_PINNED_MEMORY:-auto}"
  case "$pinned_mode" in
    true)
      flags+=("--disable-pinned-memory")
      log_info "$(t opt_pinned_forced_true)"
      ;;
    false)
      log_info "$(t opt_pinned_forced_false)"
      ;;
    auto|*)
      if (( SYSTEM_RAM_LIMIT_GB < H3_MIN_RAM_FOR_PINNED_MEMORY_GB )); then
        flags+=("--disable-pinned-memory")
        log_warn "$(t opt_pinned_auto_disabled "$SYSTEM_RAM_LIMIT_GB" "$SYSTEM_RAM_LIMIT_SOURCE" "$H3_MIN_RAM_FOR_PINNED_MEMORY_GB")"
      else
        log_info "$(t opt_pinned_auto_enabled "$SYSTEM_RAM_LIMIT_GB" "$SYSTEM_RAM_LIMIT_SOURCE" "$H3_MIN_RAM_FOR_PINNED_MEMORY_GB")"
      fi
      ;;
  esac

  # Empirical optimization.
  #
  # During extensive RunPod testing (RTX A6000 48 GB + >500 GB system RAM),
  # enabling --disable-smart-memory significantly improved the stability of
  # long MiniMax H3 generations (up to 15 s at 1920x1088) — see CHANGELOG.md
  # for the full test results.
  #
  # This is not an official ComfyUI recommendation and should be
  # re-evaluated as ComfyUI/comfy-kitchen memory management evolves.
  #
  # --- smart memory (cache VRAM spéculatif) selon la RAM réellement -----
  # allouée. Choix empirique basé sur les tests MiniMax H3 de ce projet, pas
  # une règle générale de gestion mémoire ComfyUI — s'applique à tout le
  # process ComfyUI, pas seulement à H3. Voir le commentaire de
  # COMFY_SMART_MEMORY dans config.env pour le contexte complet et le
  # contre-exemple RAM-contrainte à ne pas généraliser.
  local smart_memory_mode="${COMFY_SMART_MEMORY:-auto}"
  case "$smart_memory_mode" in
    true)
      flags+=("--disable-smart-memory")
      log_info "$(t opt_smart_forced_true)"
      ;;
    false)
      log_info "$(t opt_smart_forced_false)"
      ;;
    auto|*)
      if (( SYSTEM_RAM_LIMIT_GB >= H3_MIN_RAM_FOR_SMART_MEMORY_GB )); then
        flags+=("--disable-smart-memory")
        log_info "$(t opt_smart_auto_disabled "$SYSTEM_RAM_LIMIT_GB" "$SYSTEM_RAM_LIMIT_SOURCE" "$H3_MIN_RAM_FOR_SMART_MEMORY_GB")"
      else
        log_info "$(t opt_smart_auto_enabled "$SYSTEM_RAM_LIMIT_GB" "$SYSTEM_RAM_LIMIT_SOURCE" "$H3_MIN_RAM_FOR_SMART_MEMORY_GB")"
      fi
      ;;
  esac

  # --- compute capability -> --fast (accumulation fp16/fp8 rapide) ------
  local cc
  cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
  if [[ -n "$cc" ]] && awk -v c="$cc" 'BEGIN{exit !(c+0 >= 8.0)}'; then
    flags+=("--fast")
    log_info "$(t opt_fast_enabled "$cc")"
  else
    log_info "$(t opt_fast_disabled "${cc:-$(t gpu_cuda_unknown)}")"
  fi

  # --- attention backend ---------------------------------------------------
  # Voir COMFY_ATTENTION_BACKEND (config.env) pour le détail des trois modes.
  local has_xformers="false" has_flash="false"
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    has_xformers="$("${VENV_DIR}/bin/python" -c 'import importlib.util,sys; sys.stdout.write("true" if importlib.util.find_spec("xformers") else "false")' 2>/dev/null || echo false)"
    has_flash="$("${VENV_DIR}/bin/python" -c 'import importlib.util,sys; sys.stdout.write("true" if importlib.util.find_spec("flash_attn") else "false")' 2>/dev/null || echo false)"
  fi

  local attention_backend="${COMFY_ATTENTION_BACKEND:-auto}"
  case "$attention_backend" in
    pytorch)
      flags+=("--use-pytorch-cross-attention")
      if [[ "$has_xformers" == "true" ]]; then
        log_info "$(t opt_attn_pytorch_forced_xf)"
      else
        log_info "$(t opt_attn_pytorch_forced)"
      fi
      ;;
    xformers)
      if [[ "$has_xformers" == "true" ]]; then
        log_ok "$(t opt_attn_xformers_forced_ok)"
      else
        flags+=("--use-pytorch-cross-attention")
        log_error "$(t opt_attn_xformers_forced_missing)"
      fi
      ;;
    auto|*)
      if [[ "$has_xformers" == "true" ]]; then
        log_ok "$(t opt_attn_auto_xformers)"
      else
        flags+=("--use-pytorch-cross-attention")
        log_warn "$(t opt_attn_auto_missing)"
      fi
      ;;
  esac

  if [[ "$has_flash" == "true" ]]; then
    log_ok "$(t opt_flash_detected)"
  else
    log_info "$(t opt_flash_not_installed)"
  fi

  # --- écriture du fichier ---------------------------------------------
  # Écrit dans user/ (déjà ignoré par le .gitignore de ComfyUI) pour ne
  # jamais faire apparaître le dépôt comme "modifié" aux yeux d'update.sh.
  mkdir -p "${INSTALL_DIR}/user"
  local out="${INSTALL_DIR}/user/${LAUNCH_FLAGS_FILE_NAME}"
  {
    echo "# Auto-generated by lib/optimization.sh — do not edit by hand."
    echo "# GPU: ${GPU_NAME} (${GPU_VRAM_GB} GB, compute_cap ${cc:-?})"
    echo "# RAM: ${SYSTEM_RAM_LIMIT_GB} GB allocated (source: ${SYSTEM_RAM_LIMIT_SOURCE}, total host RAM: ${SYSTEM_RAM_TOTAL_GB} GB)"
    printf 'MINIMAX_ENV_VARS=(%s)\n' "$(printf '"%s" ' "${env_vars[@]}")"
    printf 'MINIMAX_LAUNCH_FLAGS=(%s)\n' "$(printf '"%s" ' "${flags[@]}")"
  } > "$out"

  log_ok "$(t opt_flags_written "$out")"
}
