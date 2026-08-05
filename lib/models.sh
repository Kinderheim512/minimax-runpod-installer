#!/usr/bin/env bash
# lib/models.sh — arborescence de modèles ComfyUI + téléchargement des poids
# MiniMax H3 officiels (dépôt Comfy-Org/MiniMax-H3, repackagé pour ComfyUI).
#
# Sources (vérifiées) :
#   https://huggingface.co/Comfy-Org/MiniMax-H3
#   https://docs.comfy.org/tutorials/video/minimax/minimax-h3
#   https://blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui
#
# Tailles indiquées à titre informatif pour l'estimation d'espace disque
# (relevées sur le dépôt HF au moment de l'écriture — vérifiées dynamiquement
# au téléchargement via la taille distante réelle, voir lib/download.sh).

create_model_folders() {
  log_step "Création de l'arborescence de modèles"
  local base="${INSTALL_DIR}/models"
  for d in checkpoints diffusion_models text_encoders vae clip controlnet loras upscale_models; do
    mkdir -p "${base}/${d}"
  done
  log_ok "Dossiers models/{checkpoints,diffusion_models,text_encoders,vae,clip,controlnet,loras,upscale_models} prêts."
}

# --- Manifeste des fichiers H3 -----------------------------------------------
# format : "sous_chemin_dans_le_repo|palier|go_approx"
H3_DIFFUSION_FL2VA=(
  "diffusion_models/minimax_h3_fl2va_bf16.safetensors|max|66.3"
  "diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors|balanced|34"
  "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors|light|21"
)
H3_DIFFUSION_REF2VA=(
  "diffusion_models/minimax_h3_ref2va_bf16.safetensors|max|66.3"
  "diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors|balanced|34"
  "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors|light|21"
)
H3_TEXT_ENCODER=(
  "text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors|max|51.5"
  "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors|balanced|27.1"
  "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|light|15.7"
)
H3_VAE=(
  "vae/minimax_h3_video_vae_fp16.safetensors"
  "vae/minimax_h3_audio_vae_fp32.safetensors"
)

_pick_for_tier() {
  # _pick_for_tier <tier> <array_name> -> écho le sous-chemin correspondant
  local tier="$1"; shift
  local -n arr="$1"
  for entry in "${arr[@]}"; do
    IFS='|' read -r subpath t size <<< "$entry"
    if [[ "$t" == "$tier" ]]; then echo "$subpath|$size"; return 0; fi
  done
  return 1
}

resolve_h3_tier() {
  local tier="$H3_TIER"
  if [[ "$tier" == "auto" ]]; then
    tier="${GPU_TIER_RECOMMENDED:-balanced}"
    log_info "H3_TIER=auto → palier '${tier}' choisi selon la VRAM détectée."
  fi
  echo "$tier"
}

estimate_download_size_gb() {
  local tier="$1"
  local workflows="$2"
  local total=0

  IFS=',' read -ra wf <<< "${workflows/all/t2v,i2v,r2v}"
  local need_fl2va="false" need_ref2va="false"
  for w in "${wf[@]}"; do
    case "$w" in t2v|i2v) need_fl2va="true";; r2v) need_ref2va="true";; esac
  done

  local entry size
  if [[ "$need_fl2va" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"; size="${entry##*|}"
    total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
  fi
  if [[ "$need_ref2va" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"; size="${entry##*|}"
    total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
  fi
  entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"; size="${entry##*|}"
  total=$(awk -v a="$total" -v b="$size" 'BEGIN{print a+b}')
  total=$(awk -v a="$total" 'BEGIN{print a+3}')  # marge ~3 Go pour les deux VAE

  echo "$total"
}

download_h3_models() {
  log_step "Téléchargement des modèles MiniMax H3"

  local tier; tier="$(resolve_h3_tier)"
  local workflows="$H3_WORKFLOWS"
  [[ "$workflows" == "all" ]] && workflows="t2v,i2v,r2v"

  local est_gb; est_gb="$(estimate_download_size_gb "$tier" "$workflows")"
  local free_gb; free_gb="$(free_disk_gb "$INSTALL_DIR")"
  log_info "Palier retenu : ${tier} — workflows : ${workflows}"
  log_info "Espace requis (estimation) : ~${est_gb} Go — espace libre : ${free_gb:-inconnu} Go"

  if [[ -n "$free_gb" ]] && awk -v f="$free_gb" -v e="$est_gb" 'BEGIN{exit !(f < e)}'; then
    log_warn "Espace disque possiblement insuffisant (${free_gb} Go libres pour ~${est_gb} Go requis)."
    confirm "Continuer quand même ?" || { log_error "Téléchargement annulé par l'utilisateur."; return 1; }
  fi

  if ! hf_check_h3_access; then
    log_error "Téléchargement des modèles annulé : accès au dépôt non confirmé."
    log_error "Acceptez la licence puis relancez : bash install.sh --only-models"
    return 1
  fi

  local base="${INSTALL_DIR}/models"
  IFS=',' read -ra wf <<< "$workflows"
  local need_fl2va="false" need_ref2va="false"
  for w in "${wf[@]}"; do
    case "$w" in
      t2v|i2v) need_fl2va="true" ;;
      r2v) need_ref2va="true" ;;
      *) log_warn "Workflow inconnu ignoré : ${w}" ;;
    esac
  done

  local entry subpath
  if [[ "$need_fl2va" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_FL2VA)"; subpath="${entry%%|*}"
download_hf_file "$H3_HF_REPO" "$subpath" "$base"
  fi
  if [[ "$need_ref2va" == "true" ]]; then
    entry="$(_pick_for_tier "$tier" H3_DIFFUSION_REF2VA)"; subpath="${entry%%|*}"
download_hf_file "$H3_HF_REPO" "$subpath" "$base"
  fi

  entry="$(_pick_for_tier "$tier" H3_TEXT_ENCODER)"; subpath="${entry%%|*}"
download_hf_file "$H3_HF_REPO" "$subpath" "$base"

  for vae_path in "${H3_VAE[@]}"; do
download_hf_file "$H3_HF_REPO" "$vae_path" "$base"
  done

  log_ok "Modèles MiniMax H3 (palier ${tier}, workflows ${workflows}) téléchargés."
}
