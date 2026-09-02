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

ask_multi_choice() {
  # ask_multi_choice <title> <result_var> <default_csv> <option1> [option2...]
  # Comma-separated multi-select. Each "optionN" is "value|description".
  local title="$1" __resultvar="$2" default="$3"; shift 3
  local -a opts=("$@")
  echo ""
  echo "  ${title}"
  echo "    (comma-separated numbers, e.g. 1,3 ; Enter = default '${default}')"
  local i=1 val desc
  for o in "${opts[@]}"; do
    val="${o%%|*}"; desc="${o#*|}"
    echo "   $i) ${desc}"
    i=$((i+1))
  done
  local input
  read -r -p "  Choice [1-$((i-1)), Enter = default] : " input
  if [[ -z "$input" ]]; then
    printf -v "$__resultvar" '%s' "$default"
    return 0
  fi
  local -a sel=() tokens
  IFS=',' read -ra tokens <<< "$input"
  local t
  for t in "${tokens[@]}"; do
    t="${t// /}"
    if [[ "$t" =~ ^[0-9]+$ ]] && (( t >= 1 && t <= ${#opts[@]} )); then
      sel+=("${opts[$((t-1))]%%|*}")
    else
      echo "  Ignored token: '${t}'"
    fi
  done
  if [[ ${#sel[@]} -eq 0 ]]; then
    printf -v "$__resultvar" '%s' "$default"
    return 0
  fi
  local IFS=','
  printf -v "$__resultvar" '%s' "${sel[*]}"
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
# Parsed without `eval`: config.env is a trusted local file today, but
# eval-ing an arbitrary line from it means a corrupted/tampered config.env
# could execute arbitrary shell here. The expected format is a plain bash
# array literal, e.g. H3_PRESET_REPLACES_STANDARD_TIER=(name1 name2) — we
# only ever need the bare, unquoted preset names inside the parentheses, so
# a pattern-match extraction gives the exact same result without ever
# invoking the shell parser/executor on file content.
if [[ "$_replaces_line" =~ ^H3_PRESET_REPLACES_STANDARD_TIER=\((.*)\)$ ]]; then
  # shellcheck disable=SC2206  # word-splitting on space is intentional here
  H3_PRESET_REPLACES_STANDARD_TIER=(${BASH_REMATCH[1]})
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

WIZ_DASIWA_MODE="classic"
WIZ_DASIWA_SPEED_PATH="slow"
WIZ_DASIWA_CHECKPOINT_VARIANTS="pruned"
WIZ_DASIWA_DIRECTOR_HYBRID_VARIANT="hybrid_8turbo"

# _dasiwa_wizard_auth_warning — rappel d'authentification pour un checkpoint
# hybride (source primaire HF gated Kinderheim/private, repli CivitAI).
_dasiwa_wizard_auth_warning() {
  echo ""
  echo "  ⚠ This hybrid checkpoint is served from a gated HuggingFace repo"
  echo "    (Kinderheim/private) by default — faster than CivitAI, but the"
  echo "    account must be granted access once:"
  echo "    1. Visit https://huggingface.co/Kinderheim/private/tree/main while"
  echo "       logged in, and click \"Agree and access repository\"."
  echo "    2. Create a read token at https://huggingface.co/settings/tokens"
  echo "       and pass it as HF_TOKEN=xxxxx (the installer logs in for you)."
  echo "    Fallback CivitAI (H3_DASIWA_HYBRID_HF_REPO=\"\") needs a"
  echo "    CIVITAI_API_KEY environment variable instead (https://civitai.com/"
  echo "    user/account, \"API Keys\"). Without either token, the installer"
  echo "    prints a guided manual-download message and continues — never blocks."
}

# _dasiwa_wizard_pdd_warning — le sidecar PDD (fbjr, corrigé 29/08) requiert
# son nœud MiniMaxH3PDDLoRA, fourni par un dossier à déposer manuellement.
_dasiwa_wizard_pdd_warning() {
  echo ""
  echo "  ⚠ PDD (Parallel Decoding Distillation, official 8-step acceleration)"
  echo "    needs the MiniMaxH3PDDLoRA node. It ships as a standalone folder:"
  echo "    download comfyui_minimax_h3_pdd/ from"
  echo "    https://huggingface.co/fbjr/MiniMax-H3-Acc-LoRAs-sidecar and drop it"
  echo "    into ComfyUI/custom_nodes/. The 8-step LoRA file (pruned) is served"
  echo "    from the same repo (models/loras/). Recipe: euler, CFG 1.0, shift"
  echo "    12/3, steps on the node — see the report for the full recipe."
}

case "$WIZ_PRESET" in
  dasiwa_mmh3v12)
    # --- Mode (Option A classic / Option B director) ----------------------
    ask_choice "DaSiWa preset — mode :" WIZ_DASIWA_MODE "classic" \
      "classic|Classic MythicAlchemy — checkpoint loader, stable (recommended)" \
      "director|Director mode — hybrid checkpoint + Director nodes (C-MMH3-18, experimental)"

    if [[ "$WIZ_DASIWA_MODE" == "director" ]]; then
      # Option B : checkpoint hybride (un seul à la fois), workflow C-MMH3-18
      # épinglé, VAE int8 Kijai + pack LBH installés automatiquement.
      ask_choice "Director — hybrid checkpoint variant :" WIZ_DASIWA_DIRECTOR_HYBRID_VARIANT "hybrid_8turbo" \
        "hybrid_8turbo|Hybrid 8Turbo v1 — 8 steps (recommended)" \
        "hybrid_v1|Hybrid v1 — no distillation" \
        "hybrid_4turbo|Hybrid 4Turbo v1 — 4 steps"
      WIZ_DASIWA_SPEED_PATH="hybrid"
      WIZ_DASIWA_CHECKPOINT_VARIANTS="dasiwa_hybrid"
      _dasiwa_wizard_auth_warning
    else
      # Option A : chemin vitesse (4 chemins EXCLUSIFS) puis checkpoint(s).
      ask_choice "DaSiWa preset — speed path (4 exclusive paths) :" WIZ_DASIWA_SPEED_PATH "slow" \
        "slow|Slow — official pruned + ~25 steps (recommended)" \
        "turbo_v4|Turbo v4 — official pruned + Turbo LoRA (6-8 steps)" \
        "pdd|PDD sidecar — official pruned + PDD (4-8 steps)" \
        "hybrid|Hybrid checkpoint — DaSiWa v1 (single file, both roles)"

      case "$WIZ_DASIWA_SPEED_PATH" in
        hybrid)
          WIZ_DASIWA_CHECKPOINT_VARIANTS="dasiwa_hybrid"
          _dasiwa_wizard_auth_warning
          ;;
        turbo_v4)
          # Turbo v4 a été validé contre le pruned int8 convrot uniquement.
          WIZ_DASIWA_CHECKPOINT_VARIANTS="pruned"
          WIZ_TURBO="on"
          ;;
        pdd)
          # PDD (sidecar fbjr) : officiel pruned + node MiniMaxH3PDDLoRA
          # (installation manuelle documentée — voir rapport d'analyse).
          WIZ_DASIWA_CHECKPOINT_VARIANTS="pruned"
          _dasiwa_wizard_pdd_warning
          ;;
        slow|*)
          ask_multi_choice "Checkpoint(s) to install (multi-select) :" WIZ_DASIWA_CHECKPOINT_VARIANTS "pruned" \
            "pruned|Official pruned INT8 ConvRot — FL2VA + REF2VA (~19.5 GB each)" \
            "pruned_scaled|Official pruned FP8 scaled — FL2VA + REF2VA (~19.5 GB each)" \
            "pruned_bf16|Official pruned BF16 — FL2VA + REF2VA (~37.5 GB each)" \
            "dense_int8|Official dense INT8 ConvRot — FL2VA + REF2VA (~31.7 GB each)" \
            "dasiwa_hybrid|DaSiWa Hybrid v1 — single file, both roles (~19.5 GB)"
          if [[ ",${WIZ_DASIWA_CHECKPOINT_VARIANTS}," == *",dasiwa_hybrid,"* ]]; then
            _dasiwa_wizard_auth_warning
          fi
          ;;
      esac
    fi

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
  echo "   - Mode         : ${WIZ_DASIWA_MODE}"
  echo "   - Speed path   : ${WIZ_DASIWA_SPEED_PATH}"
  if [[ "$WIZ_DASIWA_MODE" == "director" ]]; then
    echo "   - Hybrid var.  : ${WIZ_DASIWA_DIRECTOR_HYBRID_VARIANT}"
  else
    echo "   - Checkpoint(s): ${WIZ_DASIWA_CHECKPOINT_VARIANTS}"
  fi
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
export H3_DASIWA_MODE="$WIZ_DASIWA_MODE"
export H3_DASIWA_SPEED_PATH="$WIZ_DASIWA_SPEED_PATH"
export H3_DASIWA_CHECKPOINT_VARIANTS="$WIZ_DASIWA_CHECKPOINT_VARIANTS"
export H3_DASIWA_DIRECTOR_HYBRID_VARIANT="$WIZ_DASIWA_DIRECTOR_HYBRID_VARIANT"
# Rétro-compatibilité : l'ancienne variable scalaire reste synchronisée (les
# lecteurs existants et la garde de variante historique continuent de
# fonctionner ; config.env la surcharge via H3_DASIWA_CHECKPOINT_VARIANTS).
export H3_DASIWA_CHECKPOINT_VARIANT="$([[ ",${WIZ_DASIWA_CHECKPOINT_VARIANTS}," == *",dasiwa_hybrid,"* ]] && echo dasiwa_hybrid || echo pruned)"

# Persist these choices on /workspace (RunPod's PERSISTENT volume — unlike
# everything else on this line, which only lives in this shell's exported
# environment for this one run). Without this, an automatic entrypoint
# restart (container crash/OOM/spot interruption — not a new pod) re-runs
# install.sh with a fresh environment and silently falls back to defaults,
# re-downloading ~42 GB you didn't ask for. See the matching comment in
# config.env (H3_USER_CHOICES_FILE) for the read side. Only the non-secret
# choices are written here — never HF_TOKEN/CIVITAI_API_KEY: those are
# secrets and don't belong in a plaintext file on disk. Set them in RunPod's
# "Environment Variables" tab so they also survive a restart.
if [[ "$WIZ_PRESET" == "dasiwa_mmh3v12" ]]; then
  _h3_persist_root="${INSTALL_DIR:-/workspace/ComfyUI}"
  _h3_choices_file="$(dirname "$_h3_persist_root")/.minimax_user_choices.env"
  if mkdir -p "$(dirname "$_h3_choices_file")" 2>/dev/null; then
    {
      echo "# Written by wizard.sh — DaSiWa selection, persisted so an"
      echo "# automatic container restart doesn't silently switch it back to"
      echo "# defaults. Safe to delete; re-run wizard.sh to recreate it."
      echo "H3_DASIWA_MODE=\"${WIZ_DASIWA_MODE}\""
      echo "H3_DASIWA_SPEED_PATH=\"${WIZ_DASIWA_SPEED_PATH}\""
      echo "H3_DASIWA_CHECKPOINT_VARIANTS=\"${WIZ_DASIWA_CHECKPOINT_VARIANTS}\""
      echo "H3_DASIWA_DIRECTOR_HYBRID_VARIANT=\"${WIZ_DASIWA_DIRECTOR_HYBRID_VARIANT}\""
    } > "$_h3_choices_file" 2>/dev/null || echo "  (warning: could not persist the DaSiWa choices to ${_h3_choices_file} — an automatic restart may fall back to defaults)"
  fi
  unset _h3_persist_root _h3_choices_file
fi
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
  echo "  (extra ComfyUI flags: set EXTRA_LAUNCH_ARGS in config.env, or re-run wizard.sh)"
  exit 0
fi

# --- ComfyUI launch flags --------------------------------------------------
# Three simple choices, not a curated multi-select:
#   1) automatic  — the installer's GPU-tuned flags (lib/optimization.sh)
#                   PLUS config.env's own EXTRA_LAUNCH_ARGS default. Same as
#                   not touching anything.
#   2) bare       — nothing at all: skips even the GPU-tuned flags, launches
#                   literally `python main.py --listen ... --port ...` and
#                   nothing else. Sets MINIMAX_BARE_LAUNCH=true, which
#                   launch.sh checks BEFORE loading the optimization flags
#                   file (see launch.sh) — this is the only way to get a
#                   truly empty launch, since exporting EXTRA_LAUNCH_ARGS=""
#                   alone would still fall back to config.env's own default
#                   (its "${EXTRA_LAUNCH_ARGS:-...}" treats empty the same
#                   as unset).
#   3) custom     — type any flag(s) yourself (cheat sheet below), passed
#                   through as-is via EXTRA_LAUNCH_ARGS. The GPU-tuned flags
#                   from option 1 still apply underneath — this only adds
#                   on top of them, it does not replace them.
echo ""
echo "  ComfyUI launch flags :"
echo "   1) Automatic — the installer's GPU-tuned settings only  [default]"
echo "   2) Bare — nothing at all, not even the GPU-tuned flags"
echo "   3) Custom — type any flag(s) yourself, on top of the GPU-tuned settings (list below)"
read -r -p "  Choice [1-3, Enter = 1] : " launch_flags_choice
WIZ_LAUNCH_FLAGS=""
WIZ_BARE_LAUNCH="false"
case "$launch_flags_choice" in
  2)
    WIZ_BARE_LAUNCH="true"
    ;;
  3)
    echo ""
    echo "  VRAM management (advanced) — the installer already picks ONE of these"
    echo "  automatically based on your detected GPU (see lib/optimization.sh). Only"
    echo "  type one of these if you want to override that choice for this run:"
    echo "    --highvram    Keep everything in VRAM, no offloading. Fastest if it fits."
    echo "    --lowvram     Offload aggressively to save VRAM on a tight GPU."
    echo "    --novram      Minimum-VRAM mode — slowest, most conservative."
    echo "    --cpu         Force CPU only, no GPU acceleration (debugging)."
    echo "    --gpu-only    Never offload anything to RAM/disk."
    echo "    --reserve-vram N   Reserve N GB of VRAM headroom (used with --lowvram)."
    echo "  ⚠ ComfyUI refuses to start if you combine two of these AND the installer's"
    echo "  own pick is still active — if that happens, set COMFY_HIGHVRAM=true or"
    echo "  =false in config.env instead of typing the flag here, so the installer"
    echo "  uses your choice instead of adding its own. (Or pick option 2 above —"
    echo "  Bare — to drop the installer's pick entirely and start clean.)"
    echo ""
    echo "  Other useful flags :"
    echo "    --preview-method auto   Live preview thumbnails during generation."
    echo "    --enable-cors-header    Allow cross-origin API requests (some front-ends need it)."
    echo "    --verbose DEBUG         Much more detailed console/log output."
    echo "    --disable-metadata      Don't embed the workflow JSON into saved outputs."
    echo "    --multi-user            Separate settings/queue per browser."
    echo "    --disable-auto-launch   Skip trying to open a local browser on start."
    echo "  (config.env's own default, used whenever this is left empty here or the"
    echo "  wizard isn't used at all, is: --preview-method auto --enable-cors-header)"
    echo ""
    read -r -p "  Flag(s) to launch with (Enter = none) : " WIZ_LAUNCH_FLAGS
    ;;
esac
if [[ "$WIZ_BARE_LAUNCH" == "true" ]]; then
  echo "  -> launching bare: no GPU-tuned flags, no extra flags"
elif [[ -n "$WIZ_LAUNCH_FLAGS" ]]; then
  echo "  -> launching with: ${WIZ_LAUNCH_FLAGS}"
else
  echo "  -> launching with the installer's automatic settings only"
fi
export EXTRA_LAUNCH_ARGS="$WIZ_LAUNCH_FLAGS"
export MINIMAX_BARE_LAUNCH="$WIZ_BARE_LAUNCH"

exec bash "${PROJECT_ROOT}/launch.sh" --tmux
