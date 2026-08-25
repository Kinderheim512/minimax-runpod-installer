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
  log_step "$(t gpu_detect_step)"

  if ! require_cmd nvidia-smi; then
    log_error "$(t gpu_no_nvidia_smi)"
    log_error "$(t gpu_needs_cuda)"
    exit 1
  fi

  local line
  line="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -n1)"
  if [[ -z "$line" ]]; then
    log_error "$(t gpu_no_gpu_returned)"
    exit 1
  fi

  GPU_NAME="$(echo "$line" | awk -F',' '{gsub(/^ +| +$/,"",$1); print $1}')"
  GPU_VRAM_MB="$(echo "$line" | awk -F',' '{gsub(/^ +| +$/,"",$2); print $2}')"
  GPU_DRIVER="$(echo "$line" | awk -F',' '{gsub(/^ +| +$/,"",$3); print $3}')"
  # nvidia-smi reports memory.total in MiB (binary), not decimal MB — divide
  # by 1024 (not 1000) so a card the OS/vendor calls "24 GB" (really ~24576
  # MiB) reports as 24 here too. Dividing by 1000 previously under-reported
  # VRAM by ~4-7%, which could push a GPU right at a tier threshold
  # (H3_TIER_MIN_VRAM_*_GB in config.env) into the wrong tier.
  GPU_VRAM_GB=$(( GPU_VRAM_MB / 1024 ))
  GPU_CUDA_VERSION="$(nvidia-smi | grep -oP 'CUDA Version:\s*\K[0-9.]+' | head -n1 || true)"

  log_info "$(t gpu_detected_name "$GPU_NAME")"
  log_info "$(t gpu_vram "$GPU_VRAM_GB")"
  log_info "$(t gpu_driver "$GPU_DRIVER")"
  log_info "$(t gpu_cuda_driver "${GPU_CUDA_VERSION:-$(t gpu_cuda_unknown)}")"

  local known="false"
  local k
  for k in "${GPU_KNOWN_LIST[@]}"; do
    if [[ "$GPU_NAME" == *"$k"* ]]; then known="true"; break; fi
  done
  if [[ "$known" == "true" ]]; then
    log_ok "$(t gpu_known_ok)"
  else
    log_warn "$(t gpu_unknown_card "$GPU_NAME")"
  fi

  if (( GPU_VRAM_GB < MIN_VRAM_GB )); then
    log_error "$(t gpu_vram_insufficient "$GPU_VRAM_GB" "$MIN_VRAM_GB")"
    exit 1
  fi

  # Choix du palier de poids selon la VRAM, avec marge de sécurité (seuils
  # et marge configurables dans config.env : H3_TIER_MIN_VRAM_PRUNED_GB,
  # H3_TIER_MIN_VRAM_BALANCED_GB, H3_TIER_MIN_VRAM_MAX_GB,
  # H3_TIER_VRAM_SAFETY_MARGIN_GB). La marge évite de recommander un palier
  # dont le pic d'usage réel dépasse la VRAM disponible sur les cartes tout
  # juste au seuil (ex. RTX A6000 48 Go avec le palier "max" — voir le
  # commentaire dans config.env).
  # "pruned_scaled" (repli fp8_scaled) n'apparaît volontairement PAS dans
  # cette échelle : Comfy-Org documente fp8_scaled comme un repli à
  # utiliser seulement si int8_convrot ("pruned") ne fonctionne pas — jamais
  # un choix automatique, uniquement manuel (--tier=pruned_scaled).
  local vram_for_tier=$(( GPU_VRAM_GB - H3_TIER_VRAM_SAFETY_MARGIN_GB ))
  if   (( vram_for_tier >= H3_TIER_MIN_VRAM_MAX_GB ));      then GPU_TIER_RECOMMENDED="max"
  elif (( vram_for_tier >= H3_TIER_MIN_VRAM_BALANCED_GB )); then GPU_TIER_RECOMMENDED="balanced"
  elif (( vram_for_tier >= H3_TIER_MIN_VRAM_PRUNED_GB ));   then GPU_TIER_RECOMMENDED="pruned"
  else                                                           GPU_TIER_RECOMMENDED="light"
  fi
  log_info "$(t gpu_tier_recommended "$GPU_TIER_RECOMMENDED" "$GPU_VRAM_GB" "$H3_TIER_VRAM_SAFETY_MARGIN_GB")"

  export GPU_NAME GPU_VRAM_GB GPU_DRIVER GPU_CUDA_VERSION GPU_TIER_RECOMMENDED
}

SYSTEM_RAM_LIMIT_GB=0
SYSTEM_RAM_TOTAL_GB=0
SYSTEM_RAM_LIMIT_SOURCE=""

detect_system_ram() {
  # detect_system_ram — détecte la RAM réellement disponible pour CE
  # processus, pas celle de la machine hôte physique.
  #
  # Sur RunPod (et plus généralement tout hébergeur à base de conteneurs),
  # `free -h` et /proc/meminfo à l'intérieur du pod reflètent la RAM totale
  # de la machine hôte partagée, pas la limite réellement allouée au pod —
  # qui est imposée séparément par un cgroup. Un pod peut ainsi voir "503 Go
  # libres" alors que sa limite cgroup réelle est de 50 Go : tout ce qui
  # dépasse cette limite (ex. la RAM verrouillée/"pinnée" par le chargement
  # de modèles ComfyUI, cf. compute_optimization_flags()) déclenche un
  # SIGKILL du noyau (cgroup OOM), sans traceback Python ni erreur CUDA — cas
  # diagnostiqué et confirmé sur ce projet via /sys/fs/cgroup/memory.events
  # (compteur oom_kill).
  #
  # Ordre de détection : cgroup v2 -> cgroup v1 -> repli sur la RAM totale de
  # la machine hôte (/proc/meminfo) si aucun cgroup memory n'est actif (hors
  # conteneur, ou conteneur sans limite imposée).
  log_step "$(t ram_detect_step)"

  local host_kb
  host_kb="$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
  SYSTEM_RAM_TOTAL_GB=$(( host_kb / 1024 / 1024 ))

  local limit_bytes="" source=""

  if [[ -r /sys/fs/cgroup/memory.max ]]; then
    # cgroup v2 — une seule valeur, "max" si aucune limite n'est imposée.
    local raw; raw="$(cat /sys/fs/cgroup/memory.max 2>/dev/null || true)"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
      limit_bytes="$raw"
      source="cgroup v2"
    fi
  elif [[ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]]; then
    # cgroup v1 — "illimité" est représenté par une valeur sentinelle proche
    # de 2^63 (plusieurs millions de To), jamais une vraie limite de pod : on
    # l'écarte en comparant à la RAM hôte totale plutôt qu'à une constante
    # arbitraire, ce qui reste correct quelle que soit la taille de la
    # machine hôte.
    local raw; raw="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || true)"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
      local raw_gb=$(( raw / 1024 / 1024 / 1024 ))
      if (( SYSTEM_RAM_TOTAL_GB == 0 || raw_gb <= SYSTEM_RAM_TOTAL_GB )); then
        limit_bytes="$raw"
        source="cgroup v1"
      fi
    fi
  fi

  if [[ -n "$limit_bytes" ]]; then
    SYSTEM_RAM_LIMIT_GB=$(( limit_bytes / 1024 / 1024 / 1024 ))
    SYSTEM_RAM_LIMIT_SOURCE="$source"
    log_info "$(t ram_limit_detected "$source" "$SYSTEM_RAM_LIMIT_GB" "$SYSTEM_RAM_TOTAL_GB")"
  else
    SYSTEM_RAM_LIMIT_GB="$SYSTEM_RAM_TOTAL_GB"
    SYSTEM_RAM_LIMIT_SOURCE="$(t ram_no_cgroup)"
    log_info "$(t ram_no_cgroup_msg "$SYSTEM_RAM_LIMIT_GB")"
  fi

  export SYSTEM_RAM_LIMIT_GB SYSTEM_RAM_TOTAL_GB SYSTEM_RAM_LIMIT_SOURCE
}

check_cuda_stack() {
  log_step "$(t cuda_check_step)"

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log_warn "$(t cuda_venv_missing)"
    return 0
  fi

  # Low-level diagnostic output, kept in English regardless of
  # INSTALLER_LANG (technical dump, not translated via lib/i18n.sh — python
  # doesn't share the bash t() helper).
  "${VENV_DIR}/bin/python" - <<'PYEOF'
import importlib.util, sys

def status(name):
    return "present" if importlib.util.find_spec(name) else "absent"

try:
    import torch
    print(f"[torch]   version {torch.__version__} — CUDA available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"[torch]   device 0: {torch.cuda.get_device_name(0)}")
except Exception as e:
    print(f"[torch]   not installed or errored ({e})")

print(f"[xformers]        {status('xformers')}")
print(f"[flash_attn]      {status('flash_attn')}")
print(f"[sageattention]   {status('sageattention')}")
PYEOF
}
