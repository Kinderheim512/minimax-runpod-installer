#!/usr/bin/env bash
# lib/notify.sh — notifications push via ntfy.sh (pas de compte, pas de
# token : un simple "topic" que vous choisissez et suivez depuis l'app
# ntfy sur votre téléphone). Trois évènements couverts, chacun activable
# indépendamment (config.env) :
#   - Pod prêt (ComfyUI répond enfin sur son port)
#   - Génération terminée (nouveau fichier stable dans output/)
#   - Pod inactif depuis N minutes (aucune nouvelle génération) — rappel
#     utile pour ne pas laisser un pod GPU tourner (et coûter) pour rien.
#
# Désactivé par défaut (no-op silencieux) : NTFY_TOPIC vide dans config.env.
# Auto-hébergement possible via NTFY_SERVER (défaut https://ntfy.sh).

# notify <titre> <message> [priorité] [tags]
# Point d'entrée unique, best-effort : ne bloque jamais l'appelant (timeout
# court, jamais de retry — une notification manquée n'est pas grave, un
# script d'installation bloqué sur un serveur de notif injoignable le
# serait). No-op silencieux si NTFY_TOPIC est vide.
notify() {
  local title="$1" message="$2" priority="${3:-default}" tags="${4:-}"

  [[ -n "${NTFY_TOPIC:-}" ]] || return 0
  require_cmd curl || return 0

  curl -fs --max-time 10 \
    -H "Title: ${title}" \
    -H "Priority: ${priority}" \
    ${tags:+-H "Tags: ${tags}"} \
    -d "${message}" \
    "${NTFY_SERVER:-https://ntfy.sh}/${NTFY_TOPIC}" \
    >/dev/null 2>&1 &
  # Envoyée en arrière-plan (&) : même un ntfy.sh lent ne doit jamais
  # retarder le script appelant (install.sh, launch.sh...).
}

################################################################################
# Watcher "pod prêt" — à lancer en arrière-plan (&) juste avant de démarrer
# ComfyUI (voir launch.sh). Poll le port local jusqu'à ce que ComfyUI
# réponde, notifie une seule fois, puis se termine. Abandon au bout de 10
# minutes si ComfyUI ne démarre jamais (évite un process fantôme éternel).
################################################################################

notify_pod_ready_when_up() {
  [[ -n "${NTFY_TOPIC:-}" && "${NOTIFY_ON_READY:-true}" == "true" ]] || return 0

  local url="http://127.0.0.1:${COMFYUI_PORT}"
  local waited=0
  while ! curl -fs --max-time 3 "$url" >/dev/null 2>&1; do
    sleep 3
    waited=$((waited + 3))
    [[ "$waited" -ge 600 ]] && return 0
  done

  local link=""
  [[ -n "${RUNPOD_POD_ID:-}" ]] && link="https://${RUNPOD_POD_ID}-${COMFYUI_PORT}.proxy.runpod.net"
  notify "ComfyUI prêt ✅" "Ton pod MiniMax H3 est prêt.${link:+ ${link}}" "default" "white_check_mark,rocket"
}

################################################################################
# Watcher "génération terminée" + "pod inactif" — UNE seule boucle de fond
# (évite deux scans redondants de output/) à lancer en arrière-plan (&) en
# même temps que ComfyUI (voir launch.sh). Tourne tant que le conteneur/pod
# vit ; se termine avec lui, rien à nettoyer.
#
# Détection par sondage (poll), pas inotifywait : évite d'ajouter une
# dépendance système (inotify-tools) pour une fonctionnalité optionnelle —
# NOTIFY_OUTPUT_POLL_SECONDS (config.env) règle la fréquence.
################################################################################

watch_outputs_and_notify() {
  [[ -n "${NTFY_TOPIC:-}" ]] || return 0
  [[ "${NOTIFY_ON_GENERATION:-true}" == "true" || "${NOTIFY_ON_INACTIVITY:-true}" == "true" ]] || return 0

  local dir="${INSTALL_DIR}/output"
  mkdir -p "$dir"

  local poll="${NOTIFY_OUTPUT_POLL_SECONDS:-10}"
  local inactivity_s=$(( ${NOTIFY_INACTIVITY_MINUTES:-60} * 60 ))

  local seen_file
  seen_file="$(mktemp)"
  find "$dir" -type f 2>/dev/null | sort > "$seen_file"

  local last_activity inactivity_notified
  last_activity="$(date +%s)"
  inactivity_notified="false"

  while true; do
    sleep "$poll"

    local current_file new_files
    current_file="$(mktemp)"
    find "$dir" -type f 2>/dev/null | sort > "$current_file"
    new_files="$(comm -13 "$seen_file" "$current_file" 2>/dev/null)"
    mv "$current_file" "$seen_file"

    if [[ -n "$new_files" ]]; then
      last_activity="$(date +%s)"
      inactivity_notified="false"

      if [[ "${NOTIFY_ON_GENERATION:-true}" == "true" ]]; then
        local f size1 size2
        while IFS= read -r f; do
          [[ -z "$f" ]] && continue
          # Vérifie que la taille du fichier est stable avant de notifier :
          # évite de notifier une vidéo encore en cours d'écriture par
          # ComfyUI (on la retrouvera, stable, au prochain tour sinon).
          size1="$(stat -c%s "$f" 2>/dev/null || echo 0)"
          sleep 2
          size2="$(stat -c%s "$f" 2>/dev/null || echo 0)"
          [[ "$size1" == "$size2" ]] || continue
          notify "Génération terminée 🎬" "$(basename "$f")" "default" "tada,clapper"
        done <<< "$new_files"
      fi
    fi

    if [[ "${NOTIFY_ON_INACTIVITY:-true}" == "true" \
          && "$inactivity_s" -gt 0 \
          && "$inactivity_notified" == "false" ]]; then
      local now elapsed
      now="$(date +%s)"
      elapsed=$(( now - last_activity ))
      if [[ "$elapsed" -ge "$inactivity_s" ]]; then
        notify "Pod inactif ⏸️" \
          "Aucune génération depuis $(( elapsed / 60 )) min — pense à terminate le pod si tu as fini (facturation au temps GPU)." \
          "low" "hourglass"
        inactivity_notified="true"
      fi
    fi
  done
}
