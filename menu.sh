#!/usr/bin/env bash
# menu.sh — interactive menu grouping all the project's actions.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/i18n.sh"

while true; do
  echo ""
  echo "  $(t menu_title)"
  echo "  ─────────────────────────────"
  echo "  $(t menu_0)"
  echo "  $(t menu_1)"
  echo "  $(t menu_2)"
  echo "  $(t menu_3)"
  echo "  $(t menu_4)"
  echo "  $(t menu_5)"
  echo "  $(t menu_6)"
  echo "  $(t menu_7)"
  echo "  $(t menu_8)"
  echo "  $(t menu_9)"
  echo ""
  read -r -p "$(t menu_prompt)" choice

  # "|| true" on each sub-command: under set -e, a failing (or cancelled)
  # install.sh/check.sh/uninstall.sh would otherwise kill the whole menu
  # instead of simply returning to the next prompt.
  case "$choice" in
    0) bash "${PROJECT_ROOT}/wizard.sh" || true ;;
    1) bash "${PROJECT_ROOT}/install.sh" || true ;;
    2) bash "${PROJECT_ROOT}/install.sh" --only-models || true ;;
    3) bash "${PROJECT_ROOT}/check.sh" || true ;;
    4) bash "${PROJECT_ROOT}/update.sh" || true ;;
    5) bash "${PROJECT_ROOT}/launch.sh" || true ;;
    6) bash "${PROJECT_ROOT}/launch.sh" --tmux || true ;;
    7) bash "${PROJECT_ROOT}/launch.sh" --stop || true ;;
    8) bash "${PROJECT_ROOT}/uninstall.sh" || true ;;
    9) techo menu_goodbye; exit 0 ;;
    *) techo menu_invalid ;;
  esac
done
