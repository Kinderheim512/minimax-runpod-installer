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
  "muse_director_seedhunt|muse_director_seedhunt — Director/Seed Hunt, poids pruned (remplace le palier standard)"

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
# Question retirée de l'assistant : la variable MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD/
# MINIMAX_H3_TURBO_NODE_AUTO_INSTALL ci-dessous ne pilote QUE le téléchargement
# "générique" du Turbo LoRA + son custom node dédié (MiniMaxH3TurboLoRA) —
# aucun des deux presets fournis n'en a besoin :
#   - dasiwa_mmh3v12 : le LoRA loader du workflow reste sur "None", Turbo
#     inutilisé.
#   - muse_director_seedhunt : le workflow utilise bien un Turbo LoRA actif
#     (nœud LoraLoaderModelOnly, mode=0), MAIS le fichier est déjà téléchargé
#     par le mécanisme de modèles propre à ce preset (PRESET_MUSE_DIRECTOR_
#     SEEDHUNT dans config.env, indépendant de ce toggle) et il utilise un
#     nœud stock ComfyUI, pas le custom node Turbo dédié.
# Dans les deux cas, désactiver cette question ne casse rien — figé sur
# Désactivé sans la poser.
WIZ_TURBO="off"

# --- SageAttention -------------------------------------------------------------
# Fortement déconseillé, quel que soit le preset. SAGE_ATTENTION ci-dessous ne
# pilote QUE l'installation du paquet Python sageattention (compilation
# depuis les sources, ~10-20 min) — pas son utilisation dans un workflow :
#   - dasiwa_mmh3v12 : l'attention est désormais gérée en natif par ComfyUI
#     via "ComfyKitchen Attention" (nœud Settings du workflow) ; le nœud
#     SageAttention dédié n'y est plus câblé.
#   - muse_director_seedhunt : le nœud SageAttention (PathchSageAttentionKJ,
#     id 241) est présent dans le workflow bundlé mais désactivé par défaut
#     (mode bypass) — il ne s'exécute pas tant qu'on ne l'active pas
#     manuellement dans ComfyUI.
# Dans les deux cas, installer le paquet ici n'apporte donc aucun bénéfice
# par défaut et ne fait que rallonger fortement l'installation. Question
# conservée mais désactivée par défaut, avec avertissement explicite.
echo ""
echo "  ⚠ SageAttention n'est pas utilisé par défaut dans les workflows fournis"
echo "    (DaSiWa : remplacé par ComfyKitchen Attention, natif — Director/Seed"
echo "    Hunt : nœud présent mais désactivé/bypassé par défaut). L'activer ici"
echo "    rallonge fortement le temps d'installation (compilation depuis les"
echo "    sources, ~10-20 min) pour aucun bénéfice avec les réglages par défaut."

ask_choice "SageAttention (déconseillé — non utilisé par défaut, compilation depuis les sources) :" WIZ_SAGE_ONOFF "off" \
  "off|Désactivé (recommandé)" \
  "on|Activé (rallonge fortement l'installation, sans effet par défaut)"

if [[ "$WIZ_SAGE_ONOFF" == "on" ]]; then
  WIZ_SAGE="auto"
else
  WIZ_SAGE="false"
fi

# --- Spectrum ------------------------------------------------------------------
# Question retirée de l'assistant, pour la même raison : le nœud Spectrum est
# lui aussi désactivé/bypassé par défaut dans les deux workflows bundlés
# (dasiwa_mmh3v12 comme muse_director_seedhunt) — figé sur Désactivé sans
# poser la question.
WIZ_SPECTRUM="off"

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
