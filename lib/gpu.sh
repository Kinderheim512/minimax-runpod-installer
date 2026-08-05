#!/usr/bin/env bash
# lib/gpu.sh — détection GPU, VRAM, CUDA, driver ; choix du palier de poids H3.

# GPU "connus/testés" pour affichage informatif uniquement — on ne bloque pas
# sur les cartes absentes de cette liste tant que la VRAM minimale est
# respectée (RunPod ajoute régulièrement de nouvelles références).
GPU_KNOWN_LIST=(
  "H200" "H100" "A100" "L40S" "L40" "L4" "RTX 6000 Ada" "RTX 5090" "RTX 4090"
  "RTX 4080" "A6000" "A40" "RTX 3090" "RTX 3060"
)

GPU_NAME=""
GPU_VRAM_MB=0
GPU_VRAM_GB=0
GPU_DRIVER=""
GPU_CUDA_VERSION=""
GPU_TIER_RECOMMENDED=""

detect_gpu() {
  log_step "Détection du GPU"

  if ! require_cmd nvidia-smi; then
    log_error "nvidia-smi introuvable : aucun GPU NVIDIA détecté sur ce pod."
    log_error "MiniMax H3 nécessite un GPU CUDA. Choisissez un template RunPod avec GPU NVIDIA."
    exit 1
  fi

  local line
  line="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -n1)"
  if [[ -z "$line" ]]; then
    log_error "nvidia-smi n'a retourné aucun GPU."
    exit 1
  fi

  GPU_NAME="$(echo "$line" | awk -F',' '{gsub(/^ +| +$/,"",$1); print $1}')"
  GPU_VRAM_MB="$(echo "$line" | awk -F',' '{gsub(/^ +| +$/,"",$2); print $2}')"
  GPU_DRIVER="$(echo "$line" | awk -F',' '{gsub(/^ +| +$/,"",$3); print $3}')"
  GPU_VRAM_GB=$(( GPU_VRAM_MB / 1000 ))
  GPU_CUDA_VERSION="$(nvidia-smi | grep -oP 'CUDA Version:\s*\K[0-9.]+' | head -n1 || true)"

  log_info "GPU détecté   : ${GPU_NAME}"
  log_info "VRAM          : ${GPU_VRAM_GB} Go"
  log_info "Driver NVIDIA : ${GPU_DRIVER}"
  log_info "CUDA (driver) : ${GPU_CUDA_VERSION:-inconnu}"

  local known="false"
  for k in "${GPU_KNOWN_LIST[@]}"; do
    if [[ "$GPU_NAME" == *"$k"* ]]; then known="true"; break; fi
  done
  if [[ "$known" == "true" ]]; then
    log_ok "Carte reconnue dans la liste des GPU testés."
  else
    log_warn "Carte non répertoriée dans la liste testée (${GPU_NAME}) — on continue sur la seule base de la VRAM."
  fi

  if (( GPU_VRAM_GB < MIN_VRAM_GB )); then
    log_error "VRAM insuffisante : ${GPU_VRAM_GB} Go détectés, ${MIN_VRAM_GB} Go minimum requis pour MiniMax H3."
    exit 1
  fi

  # Choix du palier de poids selon la VRAM (voir config.env pour le détail)
  if   (( GPU_VRAM_GB >= 48 )); then GPU_TIER_RECOMMENDED="max"
  elif (( GPU_VRAM_GB >= 24 )); then GPU_TIER_RECOMMENDED="balanced"
  else                                GPU_TIER_RECOMMENDED="light"
  fi
  log_info "Palier de poids H3 recommandé : ${GPU_TIER_RECOMMENDED}"

  export GPU_NAME GPU_VRAM_GB GPU_DRIVER GPU_CUDA_VERSION GPU_TIER_RECOMMENDED
}

check_cuda_stack() {
  log_step "Vérification de la pile CUDA / PyTorch"

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log_warn "Environnement virtuel non encore créé, vérification différée après l'étape Python."
    return 0
  fi

  "${VENV_DIR}/bin/python" - <<'PYEOF'
import importlib.util, sys

def status(name):
    return "présent" if importlib.util.find_spec(name) else "absent"

try:
    import torch
    print(f"[torch]   version {torch.__version__} — CUDA dispo: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"[torch]   device 0: {torch.cuda.get_device_name(0)}")
except Exception as e:
    print(f"[torch]   non installé ou en erreur ({e})")

print(f"[xformers]        {status('xformers')}")
print(f"[flash_attn]      {status('flash_attn')}")
PYEOF
}
