#!/usr/bin/env bash
# docker-build-steps-heavy.sh — exécuté UNIQUEMENT à la CONSTRUCTION de
# l'image Docker pré-installée (voir Dockerfile), jamais directement par
# l'utilisateur, ni par install.sh/update.sh (usage bash classique sur pod
# nu — inchangé, voir README).
#
# Contient exclusivement les étapes COÛTEUSES du build (apt, clone ComfyUI,
# venv + dépendances, PyTorch, wheel SageAttention) — voir
# docker-build-steps-light.sh pour le reste (ComfyUI-Manager, nœuds custom,
# dossiers de modèles). Cette séparation en deux scripts existe UNIQUEMENT
# pour permettre au Dockerfile de ne COPY-er, avant ce script, que les
# fichiers dont ces étapes coûteuses dépendent réellement (config.env,
# requirements.txt, lib/utils.sh, lib/system.sh, lib/comfyui.sh,
# lib/python.sh) — pas le reste du dépôt (README, docs, presets,
# workflows...). Avec le cache de layers Docker (content-based, via
# Buildx/BuildKit), un commit qui ne touche à AUCUN de ces fichiers ne
# réinvalide donc PAS cette étape, même s'il déclenche quand même le
# workflow GitHub Actions (voir .github/workflows/docker-build.yml pour le
# filtre `paths` qui évite même ce déclenchement dans le cas courant) —
# c'est le levier (B) documenté dans le README/CHANGELOG : par ex. modifier
# lib/manager.sh, lib/nodes.sh, lib/models.sh, un preset, un workflow JSON
# ou la doc ne refait NI l'installation des paquets système, NI le clone de
# ComfyUI, NI le venv/les dépendances, NI PyTorch, NI la compilation de
# SageAttention — seul docker-build-steps-light.sh (bien plus rapide) est
# rejoué.
#
# Ne source donc QUE les lib/*.sh dont ces étapes ont réellement besoin —
# jamais lib/manager.sh, lib/nodes.sh ou lib/models.sh ici (ce sont ceux du
# script "light") : les ajouter romprait le découpage ci-dessus en forçant
# le Dockerfile à copier ces fichiers avant cette étape, ce qui la
# réinvaliderait à chaque modification de l'un d'eux.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/docker-build.log"
mkdir -p "$(dirname "$LOG_FILE")"

# shellcheck disable=SC1091
source "${PROJECT_ROOT}/config.env"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/utils.sh"
enable_error_trap
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/system.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/comfyui.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/python.sh"

log_step "Construction de l'image Docker pré-installée — étapes coûteuses (sans GPU)"

# Signale à pip_install_requirements() (lib/python.sh) de filtrer
# torch/torchvision/torchaudio de tout requirements.txt tiers installé
# pendant ce script — aucun GPU visible ici, donc aucun moyen de choisir le
# bon index CUDA. Jamais définie par install.sh/update.sh (usage bash
# classique).
export DOCKER_BUILD_NO_TORCH=true

install_system_packages
clone_or_update_comfyui
setup_python_venv
install_comfyui_requirements_no_torch
install_extra_requirements

# Précharge PyTorch (pari cu130, voir lib/python.sh::bake_pytorch_best_guess
# pour le détail complet du raisonnement et de la sécurité du mécanisme) :
# contrairement au reste de cette image, ceci dépend potentiellement du GPU
# réel du pod — mais install_pytorch(), au démarrage du conteneur
# (docker-entrypoint.sh), vérifie et corrige automatiquement si besoin (voir
# PREFER_CUDA130, activé par défaut dans cette image, Dockerfile). Objectif :
# rendre le démarrage quasi instantané dans le cas majoritaire où le pilote
# du pod obtenu supporte déjà ce build.
bake_pytorch_best_guess

# SageAttention n'est plus pré-compilée pendant le build de l'image : cette
# étape (bake_sageattention_wheel(), lib/python.sh) installait en plus le
# toolkit CUDA complet via apt (plusieurs Go, requis pour disposer de nvcc)
# par-dessus tout ce qui est déjà téléchargé (torch, stack nvidia-*, assets
# ComfyUI), ce qui fait planter le build sur des environnements à l'espace
# disque limité — c'est exactement pour ça que SAGEATTENTION_DOCKER_BAKE=false
# était déjà forcé autour de `RUN ./docker-build-steps-heavy.sh` dans le
# Dockerfile. Reste sans impact fonctionnel : SageAttention est de toute
# façon désactivée par défaut sur les deux presets (dasiwa_mmh3v12,
# muse_director_seedhunt — voir wizard.sh), et install_sageattention()
# (lib/python.sh, appelée par install.sh au démarrage du conteneur) continue
# de fonctionner normalement si SAGE_ATTENTION=true/auto est activé
# volontairement sur un pod : elle compile alors depuis les sources à ce
# moment-là (10-20 min), exactement comme avant que ce mécanisme de
# pré-compilation n'existe.

# Marque ces étapes comme faites dans le state file
# (.minimax_installer_state, cf. lib/utils.sh::run_step/step_done), qui fait
# partie de l'image construite : au démarrage d'un conteneur, install.sh
# sautera directement ces étapes déjà réalisées plutôt que de les rejouer —
# seuls PyTorch (docker-entrypoint.sh, avant d'appeler install.sh) et tout ce
# qui suit dans install.sh (modèles H3, workflows, presets, optimisation)
# restent à faire à ce moment-là.
#
# "comfyui_requirements" est marqué fait ici alors que PyTorch n'a PAS
# encore été installé à ce stade (install_comfyui_requirements_no_torch
# l'exclut volontairement, voir lib/python.sh) : c'est intentionnel —
# docker-entrypoint.sh installe PyTorch explicitement, avec le bon index
# CUDA, AVANT de lancer install.sh. Ne jamais retirer une étape de la liste
# ci-dessous sans mettre à jour docker-entrypoint.sh en conséquence.
for step in system_packages comfyui_cloned python_venv comfyui_requirements; do
  mark_step_done "$step"
done

# --- Nettoyage (réduit la taille de CETTE couche Docker) --------------------
# Exécuté ici, dans le même script/RUN que install_system_packages et tous
# les pip install ci-dessus — donc dans la MÊME couche Docker que la
# création de ces fichiers temporaires. Un nettoyage fait dans une étape
# Docker ultérieure (ex: un RUN séparé, ou docker-build-steps-light.sh)
# n'économiserait RIEN sur la taille réelle de l'image : les couches Docker
# sont cumulatives, ce qui est écrit dans une couche antérieure doit être
# téléchargé/extrait même s'il est supprimé plus tard. PIP_NO_CACHE_DIR
# (Dockerfile) évite déjà l'essentiel côté pip ; ce qui suit couvre le
# reste (apt, résidus éventuels) — jamais exécuté par install.sh/update.sh
# sur pod nu, ce script n'étant appelé que par le Dockerfile.
log_step "Nettoyage de la couche Docker (apt, caches résiduels)"
if require_cmd apt-get; then
  sudo_cmd=""
  [[ "$(id -u)" -ne 0 ]] && require_cmd sudo && sudo_cmd="sudo"
  $sudo_cmd apt-get clean >>"$LOG_FILE" 2>&1 || true
  $sudo_cmd rm -rf /var/lib/apt/lists/* >>"$LOG_FILE" 2>&1 || true
fi
# Filet de sécurité si un outil a malgré tout écrit hors de PIP_NO_CACHE_DIR
# (ex: pip < certaines versions, ou cache d'un autre outil que pip).
rm -rf /root/.cache/pip /root/.cache/* /tmp/* 2>/dev/null || true
find "${VENV_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
log_ok "Image Docker pré-installée : étapes coûteuses terminées (nettoyée)."
