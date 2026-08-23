#!/usr/bin/env bash
# lib/lora_auto.sh — téléchargement automatique (best-effort) du Turbo LoRA
# MiniMax H3, et installation du custom node Turbo associé, lors de
# l'installation/démarrage du pod.
#
# Ne réimplémente AUCUNE logique de téléchargement : appelle install_lora.sh
# en sous-processus, exactement comme le ferait un utilisateur en ligne de
# commande (`bash install_lora.sh <URL>`). install_lora.sh reste l'unique
# source de vérité pour la résolution du nom local (--filename explicite, ou
# Content-Disposition CivitAI, ou nom d'URL directe), l'authentification
# optionnelle, la validation anti-page-HTML, la reprise/retry — rien de tout
# cela n'est dupliqué ici.
#
# install_lora.sh gère déjà lui-même l'idempotence ("LoRA already
# installed." si le fichier cible existe, téléchargement sinon) : cette
# fonction ne fait qu'orchestrer l'appel et absorber l'échec pour qu'il ne
# soit jamais bloquant, au même titre que install_sageattention()
# (lib/python.sh) — un LoRA absent ne doit jamais empêcher ComfyUI de
# démarrer.

install_turbo_lora() {
  log_step "$(t lora_turbo_step)"

  if [[ "${MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD:-true}" != "true" ]]; then
    log_info "$(t lora_turbo_disabled)"
    return 0
  fi

  if [[ -z "${MINIMAX_H3_TURBO_LORA_URL:-}" ]]; then
    log_warn "$(t lora_turbo_no_url)"
    return 0
  fi

  # Si MINIMAX_H3_TURBO_LORA_FILENAME est défini (config.env), il est
  # transmis à install_lora.sh via --filename pour imposer un nom local
  # stable (voir install_lora.sh::determine_lora_filename). Sinon,
  # comportement historique inchangé (résolution automatique du nom).
  local -a lora_args=()
  if [[ -n "${MINIMAX_H3_TURBO_LORA_FILENAME:-}" ]]; then
    lora_args+=(--filename "${MINIMAX_H3_TURBO_LORA_FILENAME}")
  fi
  lora_args+=("${MINIMAX_H3_TURBO_LORA_URL}")

  # install_lora.sh fait déjà tout le travail (idempotence, retry, auth
  # CivitAI optionnelle, validation du fichier) et se termine par exit 0/1 ;
  # appelé en sous-processus, son propre `set -e` interne ne peut pas
  # affecter celui d'install.sh/update.sh — seul son code de sortie compte
  # ici, testé par ce `if !`, donc sans risque avec `set -Eeuo pipefail`.
  if ! bash "${PROJECT_ROOT}/install_lora.sh" "${lora_args[@]}" >>"$LOG_FILE" 2>&1; then
    log_warn "$(t lora_turbo_dl_failed1 "$LOG_FILE")"
    log_warn "$(t lora_turbo_dl_failed2 "${lora_args[*]@Q}")"
    return 0
  fi

  log_ok "$(t lora_turbo_available "$INSTALL_DIR" "${MINIMAX_H3_TURBO_LORA_FILENAME:-$(t lora_turbo_filename_auto)}")"
}

# install_turbo_node
# Installe (clone) le custom node ComfyUI-MiniMax-H3-Turbo, qui fournit les
# nœuds MiniMaxH3TurboLoRA et MiniMaxH3TurboSampler attendus par les
# workflows Turbo. Volontairement séparé de install_optional_nodes()
# (lib/nodes.sh) : ce dernier exécute un `git pull --ff-only` à chaque
# passage sur tout dépôt déjà présent dans OPTIONAL_NODE_REPOS(_NO_PIP),
# alors que ce node Turbo doit rester figé une fois installé — pas de mise
# à jour automatique tant qu'elle n'est pas explicitement demandée (voir
# MINIMAX_H3_TURBO_NODE_AUTO_INSTALL, config.env). Comportement :
#   absent -> clone
#   présent -> aucune action (ni pull, ni réinstallation)
# Jamais bloquant : un échec de clonage logue un avertissement et
# n'interrompt pas install.sh/update.sh, au même titre que
# install_turbo_lora() ci-dessus.
install_turbo_node() {
  log_step "$(t node_turbo_step)"

  if [[ "${MINIMAX_H3_TURBO_NODE_AUTO_INSTALL:-true}" != "true" ]]; then
    log_info "$(t node_turbo_disabled)"
    return 0
  fi

  if [[ -z "${MINIMAX_H3_TURBO_NODE_REPO:-}" ]]; then
    log_warn "$(t node_turbo_no_repo)"
    return 0
  fi

  local nodes_dir="${INSTALL_DIR}/custom_nodes"
  local name; name="$(basename "${MINIMAX_H3_TURBO_NODE_REPO}" .git)"
  local target="${nodes_dir}/${name}"

  if [[ -d "$target" ]]; then
    log_ok "$(t node_turbo_present "$name" "$target")"
    return 0
  fi

  mkdir -p "$nodes_dir"
  log_info "$(t node_turbo_installing "$name")"
  if ! git clone "${MINIMAX_H3_TURBO_NODE_REPO}" "$target" >>"$LOG_FILE" 2>&1; then
    log_warn "$(t node_turbo_clone_failed1 "$name")"
    log_warn "$(t node_turbo_clone_failed2 "$MINIMAX_H3_TURBO_NODE_REPO" "$target")"
    return 0
  fi

  # Aucun requirements.txt dans ce dépôt (vérifié avant modification, voir
  # Turbo_LoRA_Integration_Report.md) : pas de pip install, contrairement à
  # OPTIONAL_NODE_REPOS (lib/nodes.sh) qui en installe un si présent.
  log_ok "$(t node_turbo_installed "$name" "$target")"
}
