#!/usr/bin/env bash
# wizard.sh — interactive configuration assistant.
#
# Asks a few questions (preset, weight tier, workflows, Turbo LoRA,
# SageAttention, Spectrum) then runs install.sh with the right --flags /
# environment variables. Writes nothing to config.env: the choices only
# apply to THIS run (re-running wizard.sh asks the questions again;
# config.env keeps its usual defaults for any non-interactive install —
# bootstrap.sh, curl | bash, etc. — which never goes through this script).
#
# Usage: bash wizard.sh

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Language / Langue ---------------------------------------------------
# This wizard's own prompts stay in English. This choice only controls the
# language of everything install.sh / launch.sh / lib/*.sh print during the
# actual installation and later runs (see lib/i18n.sh). Defaults to
# whatever INSTALLER_LANG already is (config.env, or an inline override
# such as `INSTALLER_LANG=fr bash wizard.sh`), so this question is skipped
# entirely when it's already set on the command line.
if [[ -z "${INSTALLER_LANG:-}" ]]; then
  echo ""
  echo "  Installer language for the rest of the install (English/Français) :"
  echo "   1) English  [default]"
  echo "   2) Français"
  read -r -p "  Choice [1-2, Enter = default] : " _lang_choice
  case "$_lang_choice" in
    2) INSTALLER_LANG="fr" ;;
    *) INSTALLER_LANG="en" ;;
  esac
  unset _lang_choice
fi
export INSTALLER_LANG

ask_choice() {
  # ask_choice <title> <result_var> <default_value> <option1> [option2...]
  # Each "optionN" is of the form "value|description".
  local title="$1" __resultvar="$2" default="$3"; shift 3
  local -a opts=("$@")
  echo ""
  echo "  ${title}"
  local i=1 val desc
  for o in "${opts[@]}"; do
    val="${o%%|*}"; desc="${o#*|}"
    if [[ "$val" == "$default" ]]; then
      echo "   $i) ${desc}  [default]"
    else
      echo "   $i) ${desc}"
    fi
    i=$((i+1))
  done
  local choice
  read -r -p "  Choice [1-$((i-1)), Enter = default] : " choice
  if [[ -z "$choice" ]]; then
    printf -v "$__resultvar" '%s' "$default"
    return 0
  fi
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#opts[@]} )); then
    val="${opts[$((choice-1))]%%|*}"
    printf -v "$__resultvar" '%s' "$val"
  else
    echo "  Invalid choice, keeping the default (${default})."
    printf -v "$__resultvar" '%s' "$default"
  fi
}

ask_workflows() {
  echo ""
  echo "  Workflows to install (comma-separated list, e.g. 1,2) :"
  echo "   1) t2v — text → video"
  echo "   2) i2v — image → video"
  echo "   3) r2v — reference → video"
  local input
  read -r -p "  Choice [1-3, Enter = all three] : " input
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
      *) echo "  Ignored token: '${t}'" ;;
    esac
  done
  if [[ ${#sel[@]} -eq 0 ]]; then
    echo "  No valid workflow selected — falling back to all three."
    sel=(t2v i2v r2v)
  fi
  local IFS=','
  WIZ_WORKFLOWS="${sel[*]}"
}

# preset_replaces_tier <preset_name> -> 0 (true) / 1 (false)
# Reads ONLY the H3_PRESET_REPLACES_STANDARD_TIER=(...) line from
# config.env (not a full `source` of the file, to avoid depending on any
# lib/*.sh function not loaded yet) — same list used by
# install.sh/lib/presets.sh, never duplicated here: if another preset is
# added tomorrow, this script adapts without modification.
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
echo "  │  Configuration Assistant — MiniMax H3               │"
echo "  └────────────────────────────────────────────────────┘"
echo "  (Enter alone = keep the default choice for each question)"

# --- Preset (asked FIRST: determines whether Tier/Workflows are relevant) ----
ask_choice "Preset (model/workflow set) :" WIZ_PRESET "dasiwa_mmh3v12" \
  "|None — standard installation only" \
  "dasiwa_mmh3v12|dasiwa_mmh3v12 — DaSiWa MythicAlchemy (replaces the standard tier)" \
  "muse_director_seedhunt|muse_director_seedhunt — Director/Seed Hunt, pruned weights (replaces the standard tier)"

if [[ -n "$WIZ_PRESET" ]] && preset_replaces_tier "$WIZ_PRESET"; then
  WIZ_TIER=""
  WIZ_WORKFLOWS=""
  echo ""
  echo "  → '${WIZ_PRESET}' provides its own set of weights and its own workflow:"
  echo "    the standard H3_TIER is not downloaded, and the official"
  echo "    t2v/i2v/r2v workflows are not installed (they would reference"
  echo "    missing weights). Tier/Workflows questions skipped."
else
  # --- H3 weight tier -----------------------------------------------------
  ask_choice "H3 weight tier (precision/VRAM) :" WIZ_TIER "auto" \
    "auto|Auto-detect based on VRAM (recommended)" \
    "light|light — reduced VRAM, lower quality/speed (~18.5 GB, third-party repo)" \
    "pruned|pruned — INT8 ConvRot, official Comfy-Org recommendation (~21 GB)" \
    "pruned_scaled|pruned_scaled — FP8 scaled, fallback if pruned doesn't work (~21 GB)" \
    "balanced|balanced — official full-precision models, pruned (~40 GB)" \
    "max|max — maximum precision, unpruned (~66 GB, 48 GB+ VRAM)"

  # --- Workflows ---------------------------------------------------------------
  ask_workflows
fi

# --- Turbo LoRA / SageAttention / Spectrum -------------------------------------
# These three options only apply to the dasiwa_mmh3v12 and
# muse_director_seedhunt presets: the official standard-tier workflows
# (t2v/i2v/r2v, no preset) don't use any of these nodes — so nothing is
# asked in that case (WIZ_PRESET empty).
#
# Turbo LoRA and Spectrum are now fixed to Disabled for both presets
# (reasons detailed in each branch below): no question asked for them
# anymore. SageAttention remains a question, but strongly discouraged, with
# a warning specific to each preset (the previous generic text — "replaced
# by ComfyKitchen Attention" — was only true for dasiwa_mmh3v12 and was
# misleading for muse_director_seedhunt).
WIZ_TURBO="off"
WIZ_SAGE_ONOFF="off"
WIZ_SAGE="false"
WIZ_SPECTRUM="off"

WIZ_DASIWA_CHECKPOINT_VARIANT="pruned"

case "$WIZ_PRESET" in
  dasiwa_mmh3v12)
    # Checkpoint variant: choice between the two official Comfy-Org pruned
    # INT8 ConvRot checkpoints (FL2VA + REF2VA, unchanged historical
    # default — 2 files) and the single community "DaSiWa Hybrid"
    # checkpoint (darksidewalker, CivitAI). The hybrid file is NOT
    # REF2VA-only: it handles both the FL2VA and REF2VA roles equally well,
    # so only 1 file is downloaded and it is symlinked under both names the
    # workflow expects. Either way the workflow itself needs no edit (see
    # PRESET_DASIWA_MMH3V12_SYMLINKS, config.env). Env var passed through to
    # install.sh below (H3_DASIWA_CHECKPOINT_VARIANT), same pattern as
    # SAGE_ATTENTION.
    ask_choice "DaSiWa preset — diffusion checkpoint(s) :" WIZ_DASIWA_CHECKPOINT_VARIANT "pruned" \
      "pruned|Normal pruned — 2 official Comfy-Org INT8 ConvRot checkpoints, FL2VA + REF2VA (recommended, well-tested)" \
      "dasiwa_hybrid|Pruned, modified by DaSiWa — 1 community checkpoint covering both FL2VA and REF2VA (darksidewalker, CivitAI, experimental)"

    if [[ "$WIZ_DASIWA_CHECKPOINT_VARIANT" == "dasiwa_hybrid" ]]; then
      echo ""
      echo "  ⚠ This CivitAI checkpoint is gated (login-required content)."
      echo "    Downloading it needs a CIVITAI_API_KEY environment variable"
      echo "    (create a key at https://civitai.com/user/account, \"API Keys\"),"
      echo "    e.g.: CIVITAI_API_KEY=xxxxx bash install.sh ..."
      echo "    Without it, the download will fail with a 401 error."
    fi

    # Turbo LoRA: the workflow's LoRA loader stays on "None" — unused,
    # no question.
    # Spectrum: no Spectrum node in this workflow — no question.
    # SageAttention: attention is now handled natively by ComfyUI via
    # "ComfyKitchen Attention" (Settings node of the workflow); the
    # dedicated SageAttention node is no longer wired in — discouraged.
    echo ""
    echo "  ⚠ SageAttention is no longer used by the DaSiWa workflow (replaced"
    echo "    by ComfyKitchen Attention, native). Enabling it here strongly"
    echo "    lengthens installation time (compiling from source, ~10-20 min)"
    echo "    for no benefit with this preset."
    ask_choice "SageAttention (discouraged — replaced by ComfyKitchen Attention in this preset) :" WIZ_SAGE_ONOFF "off" \
      "off|Disabled (recommended)" \
      "on|Enabled (lengthens installation significantly, no effect with this preset)"
    ;;
  muse_director_seedhunt)
    # Turbo LoRA: the workflow does load an active Turbo LoRA
    # (LoraLoaderModelOnly node, mode=0), BUT the file is already
    # downloaded by this preset's own model mechanism (PRESET_MUSE_
    # DIRECTOR_SEEDHUNT, config.env), independent of this toggle — and it
    # uses a stock ComfyUI node, not the dedicated Turbo custom node. No
    # question.
    # Spectrum: node present (SpectrumApplyMiniMaxH3) but disabled/bypassed
    # by default in the bundled workflow, and explicitly listed as
    # "optional" by the upstream README — no question.
    # SageAttention: node present (PathchSageAttentionKJ) but
    # disabled/bypassed by default — discouraged.
    echo ""
    echo "  ⚠ The SageAttention node in the Director/Seed Hunt workflow is"
    echo "    disabled (bypassed) by default. Enabling it here strongly"
    echo "    lengthens installation time (compiling from source, ~10-20 min)"
    echo "    for no benefit unless you also enable it manually in ComfyUI."
    ask_choice "SageAttention (discouraged — disabled by default in this preset) :" WIZ_SAGE_ONOFF "off" \
      "off|Disabled (recommended)" \
      "on|Enabled (lengthens installation significantly, no effect unless manually enabled)"
    ;;
  *)
    # Standard tier (no preset): Turbo/SageAttention/Spectrum are not used
    # by any of the official t2v/i2v/r2v workflows — nothing to ask.
    ;;
esac

if [[ "$WIZ_SAGE_ONOFF" == "on" ]]; then
  WIZ_SAGE="auto"
else
  WIZ_SAGE="false"
fi

echo ""
echo "  Summary :"
echo "   - Preset       : ${WIZ_PRESET:-none}"
if [[ "$WIZ_PRESET" == "dasiwa_mmh3v12" ]]; then
  echo "   - Checkpoint(s): ${WIZ_DASIWA_CHECKPOINT_VARIANT}"
fi
if [[ -n "$WIZ_TIER" ]]; then
  echo "   - Tier         : ${WIZ_TIER}"
  echo "   - Workflows    : ${WIZ_WORKFLOWS}"
else
  echo "   - Tier         : (n/a — provided by the preset)"
  echo "   - Workflows    : (n/a — provided by the preset)"
fi
echo "   - Turbo LoRA   : ${WIZ_TURBO}"
echo "   - SageAttention: ${WIZ_SAGE_ONOFF}"
echo "   - Spectrum     : ${WIZ_SPECTRUM}"
echo ""
read -r -p "  Start the installation with these settings? [Y/n] " confirm
if [[ "$confirm" =~ ^[nN] ]]; then
  echo "  Cancelled."
  exit 0
fi

# Turbo LoRA: two separate switches in config.env (LoRA download + auto-
# install of the associated custom node) — the wizard question covers both
# at once, which is simpler to understand.
if [[ "$WIZ_TURBO" == "on" ]]; then
  export MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD="true"
  export MINIMAX_H3_TURBO_NODE_AUTO_INSTALL="true"
else
  export MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD="false"
  export MINIMAX_H3_TURBO_NODE_AUTO_INSTALL="false"
fi
export SAGE_ATTENTION="$WIZ_SAGE"
export H3_DASIWA_CHECKPOINT_VARIANT="$WIZ_DASIWA_CHECKPOINT_VARIANT"
if [[ "$WIZ_SPECTRUM" == "on" ]]; then
  export INSTALL_SPECTRUM="true"
else
  export INSTALL_SPECTRUM="false"
fi

install_args=()
if [[ -n "$WIZ_TIER" ]]; then
  install_args+=(--tier="$WIZ_TIER" --workflows="$WIZ_WORKFLOWS")
fi
# Otherwise (a preset that replaces the standard tier): pass neither
# --tier= nor --workflows= at all, since these settings have no effect in
# that case (standard download + official workflow copy are skipped on the
# install.sh side — see preset_replaces_standard_tier()); leaving them at
# their config.env defaults avoids an empty --workflows=, which would fall
# back to "t2v,i2v,r2v" by default (see resolve_h3_workflows()).
if [[ -n "$WIZ_PRESET" ]]; then
  install_args+=(--preset="$WIZ_PRESET")
else
  # Explicitly force "no preset", even if config.env has a default
  # (dasiwa_mmh3v12) — otherwise a missing --preset= would let config.env's
  # default apply silently, contradicting the "None" choice made above.
  install_args+=(--preset=)
fi

# No "exec" here: we need the exit code to decide whether to offer
# launching ComfyUI right after.
if ! bash "${PROJECT_ROOT}/install.sh" "${install_args[@]}"; then
  install_status=$?
  echo ""
  echo "  Installation failed (code ${install_status}) — see the messages above / logs/install.log."
  exit "$install_status"
fi

echo ""
read -r -p "  Launch ComfyUI now (tmux session)? [Y/n] " launch_confirm
if [[ "$launch_confirm" =~ ^[nN] ]]; then
  echo "  Done. To launch later: bash launch.sh --tmux"
  exit 0
fi
exec bash "${PROJECT_ROOT}/launch.sh" --tmux
