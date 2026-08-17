#!/usr/bin/env bash
# wizard.sh — assistant de configuration interactif.
#
# Pose quelques questions (preset, palier de poids, workflows, Turbo LoRA,
# SageAttention, Spectrum) puis lance install.sh avec les bons --flags /
# variables d'environnement. N'écrit rien dans config.env : les choix ne
# valent que pour CETTE exécution (relancer wizard.sh pose à nouveau les
# questions ; config.env garde ses valeurs par défaut habituelles pour toute
# installation non-interactive — bootstrap.sh, curl | bash, etc. — qui ne
# passe jamais par ce script).
#
# Usage : bash wizard.sh

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Protection tmux -------------------------------------------------------
# install.sh peut prendre très longtemps (téléchargements de modèles,
# repli PyTorch cu130 -> build compatible, compilation SageAttention...).
# Le terminal web RunPod peut se déconnecter pendant ce temps (cf.
# TMUX.md) ; sans protection, install.sh tourne comme processus enfant du
# shell qui a lancé wizard.sh et meurt avec lui (SIGHUP) si ce terminal
# disparaît — d'où des installations qui "plantent" systématiquement au
# même endroit (la phase la plus longue) sans rapport avec la logique de
# repli PyTorch elle-même, qui fonctionne correctement.
#
# Nom de session DÉLIBÉRÉMENT différent de "minimax" (utilisée par
# launch.sh --tmux pour ComfyUI lui-même) : les deux sessions ont des
# rôles différents et ne doivent jamais se marcher dessus — si "minimax"
# tourne déjà (ComfyUI actif), ce wrapper ne doit pas s'y attacher à sa
# place.
WIZARD_TMUX_SESSION_NAME="minimax-install"

_wizard_attach_tmux_if_interactive() {
  # Même logique que attach_tmux_if_interactive() dans launch.sh (non
  # factorisée entre les deux scripts : chacun reste autonome / lisible
  # isolément, et la logique est courte).
  #
  # CAS IMBRIQUÉ : si ce script tourne déjà DANS un client tmux attaché à
  # une AUTRE session (ex. le terminal web RunPod ouvre lui-même une
  # session par défaut), un "tmux attach-session" classique refuse de
  # s'imbriquer et échoue silencieusement à cause du "exec" -> on utilise
  # "switch-client" à la place, qui change simplement la session affichée
  # par le client tmux courant.
  if [[ -n "${TMUX:-}" ]]; then
    exec tmux switch-client -t "$WIZARD_TMUX_SESSION_NAME"
  fi

  if [[ -t 0 && -t 1 ]]; then
    exec tmux attach-session -t "$WIZARD_TMUX_SESSION_NAME"
  fi

  # Pas de TTY (curl | bash, Start Command RunPod non interactif...) :
  # wizard.sh a de toute façon besoin d'un terminal interactif pour ses
  # questions (read -r -p) et échouerait plus loin — on laisse la session
  # tourner en arrière-plan et on indique comment s'y rattacher, plutôt
  # que de faire échouer un exec sans TTY.
  echo "[INFO] Pas de terminal interactif détecté — la session tmux '${WIZARD_TMUX_SESSION_NAME}' continue de tourner en arrière-plan."
  echo "[INFO] Pour vous y rattacher : tmux attach -t ${WIZARD_TMUX_SESSION_NAME}"
  exit 0
}

# Ne se déclenche que si CE script n'est pas déjà celui qui tourne dans la
# session (évite une boucle infinie : la session lance "bash wizard.sh",
# qui ré-atteint ce bloc, voit $TMUX défini -> passe simplement à la
# suite du script au lieu de re-créer une session).
if [[ -z "${TMUX:-}" && "${WIZARD_SKIP_TMUX_WRAP:-false}" != "true" ]]; then
  if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$WIZARD_TMUX_SESSION_NAME" 2>/dev/null; then
      echo "[INFO] Session tmux '${WIZARD_TMUX_SESSION_NAME}' déjà existante — attache..."
      _wizard_attach_tmux_if_interactive
    fi
    echo "[INFO] Lancement de l'assistant dans une session tmux ('${WIZARD_TMUX_SESSION_NAME}') pour survivre à une déconnexion du terminal RunPod..."
    # WIZARD_SKIP_TMUX_WRAP=true : la session relance ce même script sans
    # reboucler sur ce bloc (cf. condition ci-dessus). "$@" propage les
    # éventuels arguments de cet appel de wizard.sh à celui qui tourne
    # dans la session.
    tmux new-session -d -s "$WIZARD_TMUX_SESSION_NAME" \
      "WIZARD_SKIP_TMUX_WRAP=true bash \"${BASH_SOURCE[0]}\" $*; exec bash"
    _wizard_attach_tmux_if_interactive
  else
    echo "[ATTENTION] tmux introuvable — l'assistant va tourner sans protection contre une déconnexion du terminal (il devrait pourtant être installé automatiquement, cf. lib/system.sh)."
  fi
fi

ask_choice() {
  # ask_choice <titre> <var_résultat> <valeur_par_défaut> <option1> [option2...]
  # Chaque "optionN" est de la forme "valeur|description".
  local title="$1" __resultvar="$2" default="$3"; shift 3
  local -a opts=("$@")
  echo ""
  echo "  ${title}"
  local i=1 val desc
  for o in "${opts[@]}"; do
    val="${o%%|*}"; desc="${o#*|}"
    if [[ "$val" == "$default" ]]; then
      echo "   $i) ${desc}  [défaut]"
    else
      echo "   $i) ${desc}"
    fi
    i=$((i+1))
  done
  local choice
  read -r -p "  Choix [1-$((i-1)), Entrée = défaut] : " choice
  if [[ -z "$choice" ]]; then
    printf -v "$__resultvar" '%s' "$default"
    return 0
  fi
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#opts[@]} )); then
    val="${opts[$((choice-1))]%%|*}"
    printf -v "$__resultvar" '%s' "$val"
  else
    echo "  Choix invalide, valeur par défaut retenue (${default})."
    printf -v "$__resultvar" '%s' "$default"
  fi
}

ask_workflows() {
  echo ""
  echo "  Workflows à installer (liste séparée par des virgules, ex : 1,2) :"
  echo "   1) t2v — texte → vidéo"
  echo "   2) i2v — image → vidéo"
  echo "   3) r2v — référence → vidéo"
  local input
  read -r -p "  Choix [1-3, Entrée = les trois] : " input
  if [[ -z "$input" ]]; then
    WIZ_WORKFLOWS="t2v,i2v,r2v"
    return
  fi
  local -a sel=() tokens
  IFS=',' read -ra tokens <<< "$input"
  local t
  for t in "${tokens[@]}"; do
    t="${t// /}"
    case "$t" in
      1) sel+=("t2v") ;;
      2) sel+=("i2v") ;;
      3) sel+=("r2v") ;;
      "") ;;
      *) echo "  Jeton ignoré : '${t}'" ;;
    esac
  done
  if [[ ${#sel[@]} -eq 0 ]]; then
    echo "  Aucun workflow valide sélectionné — repli sur les trois."
    sel=(t2v i2v r2v)
  fi
  local IFS=','
  WIZ_WORKFLOWS="${sel[*]}"
}

# preset_replaces_tier <nom_preset> -> 0 (true) / 1 (false)
# Lit UNIQUEMENT la ligne H3_PRESET_REPLACES_STANDARD_TIER=(...) de
# config.env (pas un `source` complet du fichier, pour ne dépendre d'aucune
# fonction lib/*.sh non encore chargée) — même liste que celle utilisée par
# install.sh/lib/presets.sh, jamais dupliquée ici : si demain un autre
# preset y est ajouté, ce script s'adapte sans modification.
declare -a H3_PRESET_REPLACES_STANDARD_TIER=()
_replaces_line="$(grep -E '^H3_PRESET_REPLACES_STANDARD_TIER=' "${PROJECT_ROOT}/config.env" || true)"
if [[ -n "$_replaces_line" ]]; then
  eval "$_replaces_line"
fi

preset_replaces_tier() {
  local p="$1" x
  for x in "${H3_PRESET_REPLACES_STANDARD_TIER[@]:-}"; do
    [[ -n "$x" && "$x" == "$p" ]] && return 0
  done
  return 1
}

echo ""
echo "  ┌────────────────────────────────────────────────────┐"
echo "  │  Assistant de configuration — MiniMax H3            │"
echo "  └────────────────────────────────────────────────────┘"
echo "  (Entrée seule = garder le choix par défaut à chaque question)"

# --- Preset (posé EN PREMIER : conditionne si Palier/Workflows ont un sens) --
ask_choice "Preset (jeu de modèles/workflow) :" WIZ_PRESET "dasiwa_mmh3v12" \
  "|Aucun — installation standard uniquement" \
  "dasiwa_mmh3v12|dasiwa_mmh3v12 — DaSiWa MythicAlchemy (remplace le palier standard)" \
  "aistudynow|aistudynow — checkpoint expérimental W4A8 (additif)" \
  "minimaxh3auto_v5|minimaxh3auto_v5 (additif)"

if [[ -n "$WIZ_PRESET" ]] && preset_replaces_tier "$WIZ_PRESET"; then
  WIZ_TIER=""
  WIZ_WORKFLOWS=""
  echo ""
  echo "  → '${WIZ_PRESET}' fournit son propre jeu de poids et son propre workflow :"
  echo "    le palier standard H3_TIER n'est pas téléchargé, et les workflows"
  echo "    officiels t2v/i2v/r2v ne sont pas installés (ils référenceraient des"
  echo "    poids absents). Questions Palier/Workflows sautées."
else
  # --- Palier de poids H3 -----------------------------------------------------
  ask_choice "Palier de poids H3 (précision/VRAM) :" WIZ_TIER "auto" \
    "auto|Auto-détection selon la VRAM (recommandé)" \
    "light|light — VRAM réduite, qualité/vitesse moindres (~18,5 Go, dépôt tiers)" \
    "pruned|pruned — INT8 ConvRot, recommandation officielle Comfy-Org (~21 Go)" \
    "pruned_scaled|pruned_scaled — FP8 scaled, repli si pruned ne fonctionne pas (~21 Go)" \
    "balanced|balanced — modèles officiels pleine précision, élagués (~40 Go)" \
    "max|max — précision maximale, non élagué (~66 Go, 48 Go+ VRAM)"

  # --- Workflows ---------------------------------------------------------------
  ask_workflows
fi

# --- Turbo LoRA ---------------------------------------------------------------
ask_choice "Turbo LoRA MiniMax H3 (génération accélérée) :" WIZ_TURBO "on" \
  "on|Activé (téléchargement + custom node auto)" \
  "off|Désactivé"

# --- SageAttention -------------------------------------------------------------
# On/Off comme les autres options — mais "On" = SAGE_ATTENTION=auto (tente
# seulement si le GPU est compatible, jamais bloquant en cas d'échec), pas
# =true (qui forcerait la compilation même sur un GPU non recommandé). Le
# seuil et le compute capability réel du GPU sont lus pour prévenir l'
# utilisateur AVANT qu'il choisisse, plutôt que de le laisser découvrir un
# échec de compilation après coup.
_cc_line="$(grep -E '^SAGEATTENTION_MIN_COMPUTE_CAP=' "${PROJECT_ROOT}/config.env" || true)"
[[ -n "$_cc_line" ]] && eval "$_cc_line"
SAGEATTENTION_MIN_COMPUTE_CAP="${SAGEATTENTION_MIN_COMPUTE_CAP:-8.0}"

WIZ_GPU_CC=""
if command -v nvidia-smi >/dev/null 2>&1; then
  WIZ_GPU_CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
fi

SAGE_DEFAULT="on"
if [[ -z "$WIZ_GPU_CC" ]] || ! awk -v c="$WIZ_GPU_CC" -v m="$SAGEATTENTION_MIN_COMPUTE_CAP" 'BEGIN{exit !(c+0 >= m+0)}'; then
  echo ""
  echo "  ⚠ GPU (compute capability ${WIZ_GPU_CC:-inconnue}) sous le seuil recommandé (${SAGEATTENTION_MIN_COMPUTE_CAP}) pour SageAttention."
  echo "    La compilation pourrait échouer ou être instable sur ce GPU — mieux vaut choisir Désactivé."
  SAGE_DEFAULT="off"
fi

ask_choice "SageAttention (attention optimisée, compilation depuis les sources) :" WIZ_SAGE_ONOFF "$SAGE_DEFAULT" \
  "on|Activé" \
  "off|Désactivé"

if [[ "$WIZ_SAGE_ONOFF" == "on" ]]; then
  WIZ_SAGE="auto"
else
  WIZ_SAGE="false"
fi

# --- Spectrum ------------------------------------------------------------------
ask_choice "Spectrum (nœud d'accélération optionnel pour MiniMax H3) :" WIZ_SPECTRUM "on" \
  "on|Activé" \
  "off|Désactivé"

echo ""
echo "  Récapitulatif :"
echo "   - Preset       : ${WIZ_PRESET:-aucun}"
if [[ -n "$WIZ_TIER" ]]; then
  echo "   - Palier       : ${WIZ_TIER}"
  echo "   - Workflows    : ${WIZ_WORKFLOWS}"
else
  echo "   - Palier       : (n/a — fourni par le preset)"
  echo "   - Workflows    : (n/a — fourni par le preset)"
fi
echo "   - Turbo LoRA   : ${WIZ_TURBO}"
echo "   - SageAttention: ${WIZ_SAGE_ONOFF}"
echo "   - Spectrum     : ${WIZ_SPECTRUM}"
echo ""
read -r -p "  Lancer l'installation avec ces réglages ? [O/n] " confirm
if [[ "$confirm" =~ ^[nN] ]]; then
  echo "  Annulé."
  exit 0
fi

# Turbo LoRA : deux interrupteurs distincts dans config.env (téléchargement
# du LoRA + auto-install du custom node associé) — la question du wizard les
# couvre tous les deux en même temps, c'est plus simple à comprendre.
if [[ "$WIZ_TURBO" == "on" ]]; then
  export MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD="true"
  export MINIMAX_H3_TURBO_NODE_AUTO_INSTALL="true"
else
  export MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD="false"
  export MINIMAX_H3_TURBO_NODE_AUTO_INSTALL="false"
fi
export SAGE_ATTENTION="$WIZ_SAGE"
if [[ "$WIZ_SPECTRUM" == "on" ]]; then
  export INSTALL_SPECTRUM="true"
else
  export INSTALL_SPECTRUM="false"
fi

install_args=()
if [[ -n "$WIZ_TIER" ]]; then
  install_args+=(--tier="$WIZ_TIER" --workflows="$WIZ_WORKFLOWS")
fi
# Sinon (preset qui remplace le palier standard) : on ne passe ni --tier=
# ni --workflows= du tout, ces réglages n'ayant aucun effet dans ce cas
# (téléchargement standard + copie des workflows officiels sautés côté
# install.sh — voir preset_replaces_standard_tier()) ; les laisser à leurs
# valeurs par défaut de config.env évite un --workflows= vide qui, lui,
# retomberait sur "t2v,i2v,r2v" par défaut (voir resolve_h3_workflows()).
if [[ -n "$WIZ_PRESET" ]]; then
  install_args+=(--preset="$WIZ_PRESET")
else
  # Force explicitement "pas de preset", même si config.env a un défaut
  # (dasiwa_mmh3v12) — sinon --preset= absent laisserait le défaut de
  # config.env s'appliquer silencieusement, ce qui contredirait le choix
  # "Aucun" fait ci-dessus.
  install_args+=(--preset=)
fi

# Pas de "exec" ici : on a besoin du code de sortie pour décider de proposer
# (ou non) le lancement de ComfyUI juste après.
if ! bash "${PROJECT_ROOT}/install.sh" "${install_args[@]}"; then
  install_status=$?
  echo ""
  echo "  L'installation a échoué (code ${install_status}) — voir les messages ci-dessus / logs/install.log."
  exit "$install_status"
fi

echo ""
read -r -p "  Lancer ComfyUI maintenant (session tmux) ? [O/n] " launch_confirm
if [[ "$launch_confirm" =~ ^[nN] ]]; then
  echo "  Terminé. Pour lancer plus tard : bash launch.sh --tmux"
  exit 0
fi
exec bash "${PROJECT_ROOT}/launch.sh" --tmux
