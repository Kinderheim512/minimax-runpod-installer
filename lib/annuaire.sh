#!/usr/bin/env bash
# lib/annuaire.sh — téléchargements de l'annuaire du launcher OpenFox Forge
# (LoRA / Workflow / Node), pilotés par trois variables d'environnement du
# pod, une entrée par ligne (lignes vides et commençant par '#' ignorées) :
#   H3_CUSTOM_LORAS      : "<url>" ou "<url> <nom-de-fichier>"
#   H3_CUSTOM_NODES      : "<url-repo-git>" (ou "<url> false" pour sauter pip)
#   H3_CUSTOM_WORKFLOWS  : "<url> <nom-de-fichier.json>"
#
# Réutilise install_lora.sh (LoRA, --personal) et _clone_or_update_node_repo
# (lib/nodes.sh) — aucune logique de téléchargement/clonage dupliquée ici.
# Les workflows (fichiers .json) sont téléchargés dans
# user/default/workflows/personal/ (dossier perso, comme le coffre HF).
#
# Best-effort et non bloquant : l'échec d'une entrée n'interrompt jamais
# l'installation, au même titre que install_turbo_lora (lib/lora_auto.sh) et
# le traitement des manifestes (lib/personal_storage.sh).

install_custom_loras() {
  local raw="${H3_CUSTOM_LORAS:-}"
  [[ -z "$raw" ]] && return 0

  log_info "$(t annuaire_loras_header)"

  # --- 1. Lecture des entrées ---------------------------------------------
  # Deux entrées déclarant le MÊME nom de fichier explicite s'écraseraient
  # l'une l'autre. En séquentiel la seconde voyait le fichier déjà là et se
  # sautait toute seule ; en parallèle ce serait une course. On garde donc la
  # première et on prévient — même résultat qu'avant, sans la course.
  local -a urls=() names=() seen_names=()
  local line url name
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<< "$line")"
    [[ -z "$line" || "$line" == \#* ]] && continue

    url="$(awk '{print $1}' <<< "$line")"
    name="$(awk '{print $2}' <<< "$line")"

    if [[ ! "$url" =~ ^https?:// ]]; then
      log_warn "$(t annuaire_invalid_line "$line")"
      continue
    fi

    if [[ -n "$name" ]]; then
      local duplicate="false" seen
      for seen in "${seen_names[@]+"${seen_names[@]}"}"; do
        [[ "$seen" == "$name" ]] && duplicate="true" && break
      done
      if [[ "$duplicate" == "true" ]]; then
        log_warn "$(t annuaire_lora_duplicate "$name")"
        continue
      fi
      seen_names+=("$name")
    fi

    urls+=("$url")
    names+=("$name")
  done <<< "$raw"

  local total=${#urls[@]}
  [[ "$total" -eq 0 ]] && return 0

  # --- 2. Pool borné ------------------------------------------------------
  # Un run réel : 32 LoRA / ~9 Go, 4 min 12 s en séquentiel (36 Mo/s — le
  # goulot est la limite PAR CONNEXION de CivitAI, pas le réseau du pod).
  # ANNUAIRE_LORA_JOBS (config.env, défaut 4) borne la concurrence : assez
  # pour saturer la bande passante, pas assez pour se faire rate-limiter.
  local jobs="${ANNUAIRE_LORA_JOBS:-4}"
  [[ "$jobs" =~ ^[0-9]+$ ]] || jobs=4
  (( jobs < 1 )) && jobs=1
  (( jobs > total )) && jobs=total

  local tmpdir
  tmpdir="$(mktemp -d)"
  local running=0 i
  for ((i = 0; i < total; i++)); do
    _annuaire_lora_worker "$i" "${urls[$i]}" "${names[$i]}" "$tmpdir" &
    running=$((running + 1))
    if (( running >= jobs )); then
      # `wait -n` sous set -e : sans le `|| true`, l'échec d'UNE entrée
      # avorterait tout install.sh (l'échec n'est jamais fatal ici).
      wait -n || true
      running=$((running - 1))
    fi
  done
  wait || true

  # --- 3. Rapport, dans l'ordre du manifeste ------------------------------
  local ok=0 failed=0 status
  for ((i = 0; i < total; i++)); do
    [[ -f "${tmpdir}/${i}.log" ]] && cat "${tmpdir}/${i}.log" >> "$LOG_FILE"
    status="$(cat "${tmpdir}/${i}.status" 2>/dev/null || echo 1)"
    if [[ "$status" == "0" ]]; then
      ok=$((ok + 1))
    else
      failed=$((failed + 1))
      log_warn "$(t ps_manifest_lora_install_failed "${urls[$i]}" "$LOG_FILE")"
    fi
  done
  rm -rf "$tmpdir"

  if [[ "$jobs" -gt 1 ]]; then
    log_info "$(t annuaire_loras_parallel "$jobs")"
  fi

  if [[ "$failed" -eq 0 ]]; then
    log_ok "$(t annuaire_loras_ok "$ok" "$total")"
  else
    log_warn "$(t annuaire_loras_partial "$ok" "$total" "$failed" "$LOG_FILE")"
  fi
}

# _annuaire_lora_worker <index> <url> <nom> <tmpdir>
# Un LoRA, dans son propre processus — install_lora.sh est déjà un script
# autonome, aucun état n'est partagé entre deux exécutions.
#
# Son journal est ISOLÉ (LOG_FILE redirigé vers un fichier par entrée, plus
# stdout/stderr dans le même fichier) : en parallèle, tout écrire dans le
# $LOG_FILE commun via `tee -a` produisait des lignes entrelacées illisibles.
# install_custom_loras() les concatène ensuite dans l'ordre du manifeste.
_annuaire_lora_worker() {
  local idx="$1" url="$2" name="$3" tmpdir="$4"
  local -a lora_args=(--personal)
  [[ -n "$name" ]] && lora_args+=(--filename "$name")
  lora_args+=("$url")

  local rc=0
  LOG_FILE="${tmpdir}/${idx}.log" \
    bash "${PROJECT_ROOT}/install_lora.sh" "${lora_args[@]}" \
    >>"${tmpdir}/${idx}.log" 2>&1 || rc=$?

  printf '%s' "$rc" > "${tmpdir}/${idx}.status"
  return 0
}

# annuaire_loras_after_launch
# Attache les téléchargements de LoRA à un ComfyUI DÉJÀ démarré.
#
# Les nœuds et les workflows de l'annuaire restent synchrones (un workflow
# dont un nœud manque ne se charge pas), mais les LoRA — 32 entrées, ~9 Go,
# 4 min 12 s sur un run réel — ne sont lus qu'à la GÉNÉRATION. Les faire
# attendre le lancement retardait l'UI de ces 4 minutes pour rien. launch.sh
# appelle cette fonction en arrière-plan, après le exec de ComfyUI.
annuaire_loras_after_launch() {
  [[ -n "${H3_CUSTOM_LORAS:-}" ]] || return 0

  # UN SEUL passage à la fois. install.sh se termine par
  # restart_comfyui_if_running(), qui relance launch.sh quand le manager ou les
  # nœuds ont réellement changé — et COMFY_CHANGED est vrai dès qu'une étape
  # a modifié quelque chose, ce qui est le cas courant. Sans ce verrou, chaque
  # launch.sh démarrait sa propre phase LoRA : les 32 LoRAs partaient EN DOUBLE,
  # en écrivant en même temps dans les mêmes fichiers .part (constaté en direct
  # sur un pod : deux install_lora.sh sur la même URL, 11 processus au lieu de 4).
  # Le second passage était de toute façon inutile : les fichiers déjà là sont
  # sautés, et le `mv` concurrent en faisait échouer un — donc un faux échec
  # dans le rapport final.
  local lock="${PROJECT_ROOT}/logs/annuaire-loras.lock"
  mkdir -p "$(dirname "$lock")"
  if require_cmd flock; then
    exec 9>"$lock"
    if ! flock -n 9; then
      log_info "$(t annuaire_loras_already_running)"
      return 0
    fi
  fi

  # Même sonde de disponibilité que notify_pod_ready_when_up (lib/notify.sh) :
  # on ne commence qu'une fois le serveur capable de répondre, pour ne pas
  # disputer le CPU/le disque au chargement initial des modèles.
  local url="http://127.0.0.1:${COMFYUI_PORT:-8188}"
  local waited=0
  while ! curl -fs --max-time 3 "$url" >/dev/null 2>&1; do
    sleep 3
    waited=$((waited + 3))
    [[ "$waited" -ge 600 ]] && break
  done

  log_step "$(t annuaire_loras_after_launch_step)"
  install_custom_loras
}

# install_annuaire_nodes_and_workflows
# Partie synchrone de l'annuaire : nœuds + workflows, appelée par install.sh
# avant le lancement de ComfyUI. Les LoRA sont volontairement absents (voir
# annuaire_loras_after_launch).
install_annuaire_nodes_and_workflows() {
  if [[ -z "${H3_CUSTOM_NODES:-}" && -z "${H3_CUSTOM_WORKFLOWS:-}" ]]; then
    return 0
  fi

  log_step "$(t annuaire_step)"
  install_custom_nodes
  install_custom_workflows
}

install_custom_nodes() {
  local raw="${H3_CUSTOM_NODES:-}"
  [[ -z "$raw" ]] && return 0

  log_info "$(t annuaire_nodes_header)"

  local total=0
  local line url allow_pip
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<< "$line")"
    [[ -z "$line" || "$line" == \#* ]] && continue

    url="$(awk '{print $1}' <<< "$line")"
    allow_pip="$(awk '{print $2}' <<< "$line")"
    [[ "$allow_pip" == "false" ]] || allow_pip="true"

    if [[ ! "$url" =~ ^https?:// ]]; then
      log_warn "$(t annuaire_invalid_line "$line")"
      continue
    fi

    total=$((total + 1))
    _clone_or_update_node_repo "$url" "$allow_pip"
  done <<< "$raw"

  [[ "$total" -gt 0 ]] && log_ok "$(t annuaire_nodes_ok "$total")"
}

install_custom_workflows() {
  local raw="${H3_CUSTOM_WORKFLOWS:-}"
  [[ -z "$raw" ]] && return 0

  local dest="${INSTALL_DIR}/user/default/workflows/personal"
  mkdir -p "$dest"

  log_info "$(t annuaire_workflows_header)"

  local line url name
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<< "$line")"
    [[ -z "$line" || "$line" == \#* ]] && continue

    url="$(awk '{print $1}' <<< "$line")"
    name="$(awk '{print $2}' <<< "$line")"

    if [[ ! "$url" =~ ^https?:// ]]; then
      log_warn "$(t annuaire_invalid_line "$line")"
      continue
    fi

    [[ -n "$name" ]] || name="$(basename "${url%%\?*}")"
    [[ "$name" == *.json ]] || name="${name}.json"
    local target="${dest}/${name}"

    if [[ -s "$target" ]]; then
      log_ok "$(t annuaire_workflow_present "$name")"
      continue
    fi

    # La clé CivitAI part dans l'en-tête Authorization, et UNIQUEMENT vers
    # civitai.com / civitai.red — jamais vers Hugging Face ni vers un lien
    # direct. Sans elle un workflow CivitAI restreint répond 401 et n'arrive
    # jamais (constaté en direct : « curl: (22) ... 401 » alors que la clé
    # était bien dans l'env du pod, et que les LoRA du même annuaire, eux,
    # téléchargeaient sans problème — install_lora.sh envoyait l'en-tête,
    # pas ce fichier).
    local -a auth_args=()
    if [[ -n "${CIVITAI_API_KEY:-}" ]] \
       && [[ "$url" == *civitai.com* || "$url" == *civitai.red* ]]; then
      auth_args=(-H "Authorization: Bearer ${CIVITAI_API_KEY}")
      log_info "$(t annuaire_workflow_civitai_auth "$name")"
    fi

    if curl -fsSL "${auth_args[@]+"${auth_args[@]}"}" -o "${target}.part" "$url" >>"$LOG_FILE" 2>&1; then
      mv "${target}.part" "$target"
      log_ok "$(t annuaire_workflow_downloaded "$name")"
    else
      rm -f "${target}.part"
      log_warn "$(t annuaire_workflow_failed "$url" "$LOG_FILE")"
    fi
  done <<< "$raw"
}

install_annuaire() {
  if [[ -z "${H3_CUSTOM_LORAS:-}" && -z "${H3_CUSTOM_NODES:-}" && -z "${H3_CUSTOM_WORKFLOWS:-}" ]]; then
    return 0
  fi

  log_step "$(t annuaire_step)"
  install_custom_loras
  install_custom_nodes
  install_custom_workflows
}
