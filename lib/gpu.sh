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

  # Choix du palier de poids selon la VRAM, avec marge de sécurité (seuils
  # et marge configurables dans config.env : H3_TIER_MIN_VRAM_BALANCED_GB,
  # H3_TIER_MIN_VRAM_MAX_GB, H3_TIER_VRAM_SAFETY_MARGIN_GB). La marge évite
  # de recommander un palier dont le pic d'usage réel dépasse la VRAM
  # disponible sur les cartes tout juste au seuil (ex. RTX A6000 48 Go avec
  # le palier "max" — voir le commentaire dans config.env).
  local vram_for_tier=$(( GPU_VRAM_GB - H3_TIER_VRAM_SAFETY_MARGIN_GB ))
  if   (( vram_for_tier >= H3_TIER_MIN_VRAM_MAX_GB ));      then GPU_TIER_RECOMMENDED="max"
  elif (( vram_for_tier >= H3_TIER_MIN_VRAM_BALANCED_GB )); then GPU_TIER_RECOMMENDED="balanced"
  else                                                           GPU_TIER_RECOMMENDED="light"
  fi
  log_info "Palier de poids H3 recommandé : ${GPU_TIER_RECOMMENDED} (VRAM détectée : ${GPU_VRAM_GB} Go, marge de sécurité appliquée : ${H3_TIER_VRAM_SAFETY_MARGIN_GB} Go)"

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
  log_step "Détection de la RAM système (limite réelle du pod)"

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
    log_info "Limite mémoire ${source} détectée : ${SYSTEM_RAM_LIMIT_GB} Go (RAM totale machine hôte : ${SYSTEM_RAM_TOTAL_GB} Go)."
  else
    SYSTEM_RAM_LIMIT_GB="$SYSTEM_RAM_TOTAL_GB"
    SYSTEM_RAM_LIMIT_SOURCE="hôte (aucun cgroup memory détecté)"
    log_info "Aucune limite cgroup memory détectée — RAM totale machine hôte utilisée : ${SYSTEM_RAM_LIMIT_GB} Go."
  fi

  export SYSTEM_RAM_LIMIT_GB SYSTEM_RAM_TOTAL_GB SYSTEM_RAM_LIMIT_SOURCE
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
