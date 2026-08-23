#!/usr/bin/env bash
# lib/i18n.sh — minimal i18n engine for this project's bash scripts.
#
# Every user-facing string used to be hardcoded in French directly in the
# scripts. This file replaces that with a small key/lookup system:
#   - lib/lang/fr.sh and lib/lang/en.sh each declare an associative array
#     MSG[key]="text", one entry per message, %s placeholders where a value
#     needs to be interpolated (printf semantics).
#   - t() below looks a key up in the array for the currently selected
#     language and returns the formatted string. Callers use it as:
#       log_info "$(t step_skipped "$name")"
#   - INSTALLER_LANG (config.env, or exported inline: INSTALLER_LANG=en ...)
#     picks the language. Defaults to "en". Falls back to "en" for any
#     unrecognized value instead of failing, since a wrong/typo'd
#     INSTALLER_LANG must never block an install.
#
# This file is meant to be sourced, never executed directly.
if [[ -n "${MINIMAX_I18N_LOADED:-}" ]]; then
  # shellcheck disable=SC2317  # deliberate double guard: `return` succeeds
  # when this file is sourced (the intended, only supported usage) so the
  # `exit 0` after it never actually runs then — but if someone runs this
  # file directly instead of sourcing it, `return` itself fails and `exit 0`
  # is what actually stops execution. Keep both.
  return 0 2>/dev/null || exit 0
fi
MINIMAX_I18N_LOADED=1

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# INSTALLER_LANG can already be set by config.env (sourced before this file
# in every entry point) or exported inline on the command line; both take
# priority over this default.
INSTALLER_LANG="${INSTALLER_LANG:-en}"

case "$INSTALLER_LANG" in
  fr) _i18n_file="${PROJECT_ROOT}/lib/lang/fr.sh" ;;
  en) _i18n_file="${PROJECT_ROOT}/lib/lang/en.sh" ;;
  *)
    echo "[WARN]  Unknown INSTALLER_LANG='${INSTALLER_LANG}' — falling back to 'en'." >&2
    INSTALLER_LANG="en"
    _i18n_file="${PROJECT_ROOT}/lib/lang/en.sh"
    ;;
esac

# shellcheck source=/dev/null
source "$_i18n_file"
unset _i18n_file

# t <key> [args...]
# Looks up MSG[key] in the active language and prints it, substituting any
# %s/%d placeholders with the given args (printf semantics). Unknown keys
# print the key itself instead of failing, so a missing/typo'd translation
# is visible and non-blocking rather than crashing the installer.
t() {
  local key="$1"; shift || true
  local msg="${MSG[$key]:-$key}"
  if [[ $# -gt 0 ]]; then
    # shellcheck disable=SC2059  # $msg is our own controlled format string
    printf -- "$msg" "$@"
  else
    printf '%s' "$msg"
  fi
}

# techo <key> [args...]
# Same as t(), but prints the result as a standalone line (with a trailing
# newline) directly to stdout — use this wherever the call is its own
# statement (e.g. `techo wf_copied_header`) instead of `echo "$(t
# wf_copied_header)"`, which ShellCheck flags as SC2005 (useless echo).
# Still use `log_info "$(t key)"` / `"...$(t key)..."` form for
# interpolation into a larger string; that pattern is fine and not flagged.
techo() {
  # shellcheck disable=SC2005  # intentional: this IS the one place that
  # turns t()'s no-newline output into a normal `echo`-style printed line.
  echo "$(t "$@")"
}
