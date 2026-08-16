#!/usr/bin/env bash
# docker-build-steps.sh — exécuté UNIQUEMENT à la CONSTRUCTION de l'image
# Docker pré-installée (voir Dockerfile), jamais directement par
# l'utilisateur, ni par install.sh/update.sh (usage bash classique sur pod
# nu — inchangé, voir README). Prépare tout ce qui ne dépend PAS du GPU sur
# lequel l'image finira par tourner : paquets système, clonage de ComfyUI à
# la release résolue par resolve_comfyui_target(), venv, dépendances Python
# SAUF PyTorch, ComfyUI-Manager, nœuds custom obligatoires. PyTorch, les
# poids H3 et tout ce qui dépend du GPU réellement détecté restent la
# responsabilité de docker-entrypoint.sh, exécuté au DÉMARRAGE du conteneur
# — jamais à la construction de l'image (voir Dockerfile pour le contexte
# complet : une seule image doit servir à tous les GPU RunPod).
#
# Réutilise exactement les mêmes fonctions lib/*.sh que install.sh — aucune
# commande git/pip dupliquée ici ; voir l'en-tête de chaque lib/*.sh sourcé
# ci-dessous pour le détail de chaque étape.

set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/docker-build.log"

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
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/manager.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/nodes.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/models.sh"

log_step "Construction de l'image Docker pré-installée — étapes sans GPU"

# Signale à pip_install_requirements() (lib/python.sh) de filtrer
# torch/torchvision/torchaudio de TOUT requirements.txt tiers installé
# pendant ce script (nœuds custom, ComfyUI-Manager) — même raison que
# install_comfyui_requirements_no_torch() pour celui de ComfyUI lui-même :
# aucun GPU visible ici, donc aucun moyen de choisir le bon index CUDA.
# Jamais définie par install.sh/update.sh (usage bash classique).
export DOCKER_BUILD_NO_TORCH=true

install_system_packages
clone_or_update_comfyui
setup_python_venv
install_comfyui_requirements_no_torch
install_extra_requirements
install_or_update_manager
install_optional_nodes
create_model_folders

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
for step in system_packages comfyui_cloned python_venv comfyui_requirements \
            manager_installed optional_nodes model_folders; do
  mark_step_done "$step"
done

log_ok "Image Docker pré-installée : étapes sans GPU terminées."
