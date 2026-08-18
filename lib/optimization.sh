#!/usr/bin/env bash
# lib/optimization.sh — calcule les arguments de lancement ComfyUI et les
# variables d'environnement adaptés au GPU détecté, et les écrit dans un
# fichier que launch.sh se contente de sourcer. Toutes les options utilisées
# ici sont des flags officiels documentés de ComfyUI/main.py (--lowvram,
# --highvram, --reserve-vram, --use-pytorch-cross-attention, --fast, etc.).

LAUNCH_FLAGS_FILE_NAME=".minimax_launch_flags"

compute_optimization_flags() {
  log_step "Calcul des optimisations selon le GPU"

  local flags=()
  local env_vars=()

  env_vars+=("PYTORCH_CUDA_ALLOC_CONF=expandable_segments:False")
  env_vars+=("HF_XET_HIGH_PERFORMANCE=1")

  # --- gestion mémoire selon la VRAM -------------------------------------
  # --highvram désactive tout l'offloading CPU<->GPU de ComfyUI (modèles,
  # text encoder, VAE gardés en VRAM en permanence). Sur un GPU tout juste
  # à 48 Go (RTX A6000, RTX 6000 Ada...), le pic réel (poids + activations +
  # overhead allocateur CUDA) dépasse régulièrement les 48 Go nominaux et
  # provoque un CUDA OOM systématique sur les workflows H3 Reference-to-
  # Video — --highvram est donc la cause la plus probable des OOM observés
  # sur cette classe de carte. On applique ici la même marge de sécurité que
  # pour le palier de poids H3 (H3_TIER_VRAM_SAFETY_MARGIN_GB, cf. lib/
  # gpu.sh) plutôt que le seuil brut, et on garde un contrôle explicite via
  # COMFY_HIGHVRAM dans config.env :
  #   - true  : force --highvram, quelle que soit la VRAM détectée.
  #   - false : force la gestion normale (pas de --highvram), quelle que
  #             soit la VRAM détectée.
  #   - auto  (défaut) : décide selon la VRAM détectée moins la marge de
  #             sécurité — n'active --highvram que sur les GPU qui ont une
  #             marge réelle au-dessus de 48 Go (A100/H100/H200...), pas sur
  #             les cartes tout juste à 48 Go.
  local highvram_mode="${COMFY_HIGHVRAM:-auto}"
  local vram_for_highvram=$(( GPU_VRAM_GB - H3_TIER_VRAM_SAFETY_MARGIN_GB ))

  case "$highvram_mode" in
    true)
      flags+=("--highvram")
      log_info "COMFY_HIGHVRAM=true (forcé) → --highvram (tout gardé sur GPU)."
      ;;
    false)
      if (( GPU_VRAM_GB >= 24 )); then
        flags+=("--reserve-vram" "2")
        log_info "COMFY_HIGHVRAM=false (forcé) → gestion normale + --reserve-vram 2 (pas de --highvram)."
      else
        flags+=("--lowvram" "--reserve-vram" "1")
        log_info "COMFY_HIGHVRAM=false (forcé), VRAM < 24 Go → --lowvram --reserve-vram 1."
      fi
      ;;
    auto|*)
      if (( vram_for_highvram >= 48 )); then
        flags+=("--highvram")
        log_info "COMFY_HIGHVRAM=auto, VRAM ${GPU_VRAM_GB} Go (marge de sécurité ${H3_TIER_VRAM_SAFETY_MARGIN_GB} Go appliquée) >= 48 Go → --highvram."
      elif (( GPU_VRAM_GB >= 24 )); then
        flags+=("--reserve-vram" "2")
        log_info "COMFY_HIGHVRAM=auto, VRAM ${GPU_VRAM_GB} Go (marge de sécurité appliquée) < 48 Go → gestion normale + --reserve-vram 2 (pas de --highvram)."
      else
        flags+=("--lowvram" "--reserve-vram" "1")
        log_info "COMFY_HIGHVRAM=auto, VRAM < 24 Go → --lowvram --reserve-vram 1 (offloading actif, comme documenté par l'équipe ComfyUI pour H3 sur RTX 3060)."
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
      log_info "COMFY_PINNED_MEMORY=true (forcé) → --disable-pinned-memory."
      ;;
    false)
      log_info "COMFY_PINNED_MEMORY=false (forcé) → pinning mémoire laissé actif (comportement ComfyUI par défaut)."
      ;;
    auto|*)
      if (( SYSTEM_RAM_LIMIT_GB < H3_MIN_RAM_FOR_PINNED_MEMORY_GB )); then
        flags+=("--disable-pinned-memory")
        log_warn "COMFY_PINNED_MEMORY=auto, RAM allouée ${SYSTEM_RAM_LIMIT_GB} Go (source: ${SYSTEM_RAM_LIMIT_SOURCE}) < ${H3_MIN_RAM_FOR_PINNED_MEMORY_GB} Go → --disable-pinned-memory (évite un SIGKILL cgroup pendant le chargement des modèles)."
      else
        log_info "COMFY_PINNED_MEMORY=auto, RAM allouée ${SYSTEM_RAM_LIMIT_GB} Go (source: ${SYSTEM_RAM_LIMIT_SOURCE}) >= ${H3_MIN_RAM_FOR_PINNED_MEMORY_GB} Go → pinning mémoire laissé actif."
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
      log_info "COMFY_SMART_MEMORY=true (forcé) → --disable-smart-memory."
      ;;
    false)
      log_info "COMFY_SMART_MEMORY=false (forcé) → cache VRAM spéculatif laissé actif (comportement ComfyUI par défaut)."
      ;;
    auto|*)
      if (( SYSTEM_RAM_LIMIT_GB >= H3_MIN_RAM_FOR_SMART_MEMORY_GB )); then
        flags+=("--disable-smart-memory")
        log_info "COMFY_SMART_MEMORY=auto, RAM allouée ${SYSTEM_RAM_LIMIT_GB} Go (source: ${SYSTEM_RAM_LIMIT_SOURCE}) >= ${H3_MIN_RAM_FOR_SMART_MEMORY_GB} Go → --disable-smart-memory (choix empirique basé sur les tests MiniMax H3, cf. config.env)."
      else
        log_info "COMFY_SMART_MEMORY=auto, RAM allouée ${SYSTEM_RAM_LIMIT_GB} Go (source: ${SYSTEM_RAM_LIMIT_SOURCE}) < ${H3_MIN_RAM_FOR_SMART_MEMORY_GB} Go → cache VRAM spéculatif laissé actif (RAM elle-même contrainte, cf. config.env)."
      fi
      ;;
  esac

  # --- compute capability -> --fast (accumulation fp16/fp8 rapide) ------
  local cc
  cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
  if [[ -n "$cc" ]] && awk -v c="$cc" 'BEGIN{exit !(c+0 >= 8.0)}'; then
    flags+=("--fast")
    log_info "Compute capability ${cc} >= 8.0 → --fast activé (Ampere/Ada/Hopper)."
  else
    log_info "Compute capability ${cc:-inconnue} < 8.0 (ou non détectée) → --fast non activé."
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
        log_info "COMFY_ATTENTION_BACKEND=pytorch (forcé) → --use-pytorch-cross-attention, xFormers ignoré bien qu'installé."
      else
        log_info "COMFY_ATTENTION_BACKEND=pytorch (forcé) → --use-pytorch-cross-attention."
      fi
      ;;
    xformers)
      if [[ "$has_xformers" == "true" ]]; then
        log_ok "COMFY_ATTENTION_BACKEND=xformers (forcé) → xFormers utilisé (aucun flag requis, comportement natif ComfyUI)."
      else
        flags+=("--use-pytorch-cross-attention")
        log_error "COMFY_ATTENTION_BACKEND=xformers (forcé) mais xFormers est ABSENT du venv → repli sur --use-pytorch-cross-attention. Installez xformers si vous voulez vraiment ce backend."
      fi
      ;;
    auto|*)
      if [[ "$has_xformers" == "true" ]]; then
        log_ok "xFormers détecté → laissé actif par défaut (backend d'attention)."
      else
        flags+=("--use-pytorch-cross-attention")
        log_warn "xFormers absent → repli sur --use-pytorch-cross-attention (installez xformers pour de meilleures perfs, ou passez COMFY_ATTENTION_BACKEND=pytorch pour figer ce choix explicitement)."
      fi
      ;;
  esac

  if [[ "$has_flash" == "true" ]]; then
    log_ok "Flash Attention détectée (utilisée automatiquement par les nœuds compatibles)."
  else
    log_info "Flash Attention non installée — optionnelle, non bloquante."
  fi

  # --- écriture du fichier ---------------------------------------------
  # Écrit dans user/ (déjà ignoré par le .gitignore de ComfyUI) pour ne
  # jamais faire apparaître le dépôt comme "modifié" aux yeux d'update.sh.
  mkdir -p "${INSTALL_DIR}/user"
  local out="${INSTALL_DIR}/user/${LAUNCH_FLAGS_FILE_NAME}"
  {
    echo "# Généré automatiquement par lib/optimization.sh — ne pas éditer à la main."
    echo "# GPU: ${GPU_NAME} (${GPU_VRAM_GB} Go, compute_cap ${cc:-?})"
    echo "# RAM: ${SYSTEM_RAM_LIMIT_GB} Go alloués (source: ${SYSTEM_RAM_LIMIT_SOURCE}, RAM hôte totale: ${SYSTEM_RAM_TOTAL_GB} Go)"
    printf 'MINIMAX_ENV_VARS=(%s)\n' "$(printf '"%s" ' "${env_vars[@]}")"
    printf 'MINIMAX_LAUNCH_FLAGS=(%s)\n' "$(printf '"%s" ' "${flags[@]}")"
  } > "$out"

  log_ok "Flags de lancement écrits dans ${out}"
}
