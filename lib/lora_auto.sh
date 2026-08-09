#!/usr/bin/env bash
# lib/lora_auto.sh — téléchargement automatique (best-effort) du Turbo LoRA
# MiniMax H3 lors de l'installation/démarrage du pod.
#
# Ne réimplémente AUCUNE logique de téléchargement : appelle install_lora.sh
# en sous-processus, exactement comme le ferait un utilisateur en ligne de
# commande (`bash install_lora.sh <URL>`). install_lora.sh reste l'unique
# source de vérité pour la résolution CivitAI (nom de fichier via
# Content-Disposition, authentification optionnelle, validation anti-page-
# HTML, reprise/retry) — rien de tout cela n'est dupliqué ici.
#
# install_lora.sh gère déjà lui-même l'idempotence ("LoRA already
# installed." si le fichier cible existe, téléchargement sinon) : cette
# fonction ne fait qu'orchestrer l'appel et absorber l'échec pour qu'il ne
# soit jamais bloquant, au même titre que install_sageattention()
# (lib/python.sh) — un LoRA absent ne doit jamais empêcher ComfyUI de
# démarrer.

install_turbo_lora() {
  log_step "Turbo LoRA MiniMax H3"

  if [[ "${MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD:-true}" != "true" ]]; then
    log_info "MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD=false — téléchargement du Turbo LoRA sauté."
    return 0
  fi

  if [[ -z "${MINIMAX_H3_TURBO_LORA_URL:-}" ]]; then
    log_warn "MINIMAX_H3_TURBO_LORA_URL non défini dans config.env — téléchargement du Turbo LoRA sauté."
    return 0
  fi

  # install_lora.sh fait déjà tout le travail (idempotence, retry, auth
  # CivitAI optionnelle, validation du fichier) et se termine par exit 0/1 ;
  # appelé en sous-processus, son propre `set -e` interne ne peut pas
  # affecter celui d'install.sh/update.sh — seul son code de sortie compte
  # ici, testé par ce `if !`, donc sans risque avec `set -Eeuo pipefail`.
  if ! bash "${PROJECT_ROOT}/install_lora.sh" "${MINIMAX_H3_TURBO_LORA_URL}" >>"$LOG_FILE" 2>&1; then
    log_warn "Échec du téléchargement du Turbo LoRA (CivitAI inaccessible, erreur réseau, ou autre) — non bloquant, consultez ${LOG_FILE}."
    log_warn "ComfyUI reste utilisable sans ce LoRA. Réessayez plus tard avec : bash install_lora.sh \"${MINIMAX_H3_TURBO_LORA_URL}\""
    return 0
  fi

  log_ok "Turbo LoRA MiniMax H3 disponible dans ${INSTALL_DIR}/models/loras/."
}
