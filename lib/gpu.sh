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
  log_info "Palier de poids H3 recommandé : ${GPU_TIER_RECOMMENDED} (VRAM détectée : ${GPU_VRAM_GB} Go, marge de sécurité appliquée : ${H3_TIER_VRAM_SAFETY_MARGIN_GB} Go)"

  export GPU_NAME GPU_VRAM_GB GPU_DRIVER GPU_CUDA_VERSION GPU_TIER_RECOMMENDED
}

################################################################################
# warn_if_cuda130_likely_incompatible() — à appeler juste après detect_gpu(),
# AVANT toute tentative d'installation PyTorch (lib/python.sh télécharge
# ~1-2 Go rien que pour le build cu130 avant de pouvoir constater l'échec au
# runtime). But : épargner ce temps perdu quand l'échec est quasi certain,
# sans jamais bloquer un contexte automatisé (Docker, --yes, pas de TTY) où
# personne n'est là pour répondre à une question.
#
# N'agit que si PREFER_CUDA130=true (config.env) ET qu'aucun override manuel
# explicite (TORCH_VERSION_OVERRIDE/TORCH_CUDA_INDEX_OVERRIDE) n'est déjà en
# place — dans ce dernier cas l'utilisateur a délibérément figé un build
# précis, ce garde-fou n'a rien à ajouter.
#
# Comparaison de version via `sort -V` (et non une comparaison de chaîne
# naïve) pour rester correcte sur des séquences multi-segments comme
# "570.195.03" vs "580.65.06".
################################################################################
warn_if_cuda130_likely_incompatible() {
  [[ "${PREFER_CUDA130:-false}" == "true" ]] || return 0
  [[ -z "${TORCH_VERSION_OVERRIDE:-}" && -z "${TORCH_CUDA_INDEX_OVERRIDE:-}" ]] || return 0
  [[ -n "$GPU_DRIVER" ]] || return 0

  local threshold="${CUDA130_MIN_DRIVER_VERSION:-580.65.06}"
  local smallest
  smallest="$(printf '%s\n%s\n' "$GPU_DRIVER" "$threshold" | sort -V | head -n1)"

  # Si GPU_DRIVER n'est PAS le plus petit des deux (ou est égal au seuil),
  # il est >= threshold : rien à signaler.
  [[ "$smallest" == "$GPU_DRIVER" && "$GPU_DRIVER" != "$threshold" ]] || return 0

  log_warn "PREFER_CUDA130=true, mais le driver NVIDIA détecté (${GPU_DRIVER}) est en dessous du seuil connu pour cu130 (${threshold}+ requis, cf. CUDA130_MIN_DRIVER_VERSION dans config.env)."
  log_warn "L'installation cu130 va quasi certainement échouer (CUDA indisponible au runtime), puis basculer automatiquement sur le build associé au CUDA réellement détecté (${GPU_CUDA_VERSION:-inconnu}) — ce qui fonctionne, mais fait perdre le temps de télécharger et désinstaller le build cu130 pour rien."
  log_warn "Pour obtenir un driver ≥ ${threshold} dès le départ : sur RunPod, dans l'écran de déploiement, section \"Filters\", spécifiez une version CUDA ≥ 13.0 avant de louer le pod — ce filtre porte sur le matériel alloué, pas sur ce template/config.env."

  # Contexte non-interactif (Docker, install.sh --yes, stdin fermé/pas de
  # TTY) : jamais de prompt bloquant, on continue comme avant (comportement
  # inchangé pour docker-entrypoint.sh et les runs automatisés).
  if [[ "${ASSUME_YES:-false}" == "true" || ! -t 0 ]]; then
    log_warn "Contexte non-interactif détecté — poursuite automatique avec repli cu130 -> ${GPU_CUDA_VERSION:-build détecté} (comme configuré)."
    return 0
  fi

  if ! confirm "Continuer quand même avec ce pod (repli automatique cu130 -> build compatible) ?"; then
    log_error "Installation interrompue à votre demande. Relouez un pod avec un driver ≥ ${threshold} (filtre CUDA Version côté RunPod) puis relancez wizard.sh/install.sh."
    exit 1
  fi
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
print(f"[sageattention]   {status('sageattention')}")
PYEOF
}
