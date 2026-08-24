#!/usr/bin/env bash
# docker-build-steps-light.sh — exécuté UNIQUEMENT à la CONSTRUCTION de
# l'image Docker pré-installée (voir Dockerfile), jamais directement par
# l'utilisateur, ni par install.sh/update.sh (usage bash classique sur pod
# nu — inchangé, voir README).
#
# Contient les étapes BON MARCHÉ du build (ComfyUI-Manager, nœuds custom
# obligatoires, dossiers de modèles) — voir docker-build-steps-heavy.sh pour
# les étapes coûteuses (apt, clone ComfyUI, venv/dépendances, PyTorch, wheel
# SageAttention), volontairement isolées dans un script/layer Docker
# séparé et exécuté AVANT celui-ci pour bénéficier du cache de layers
# (COPY-ées avant ce script dans le Dockerfile) : un commit qui ne touche
# QUE ce qui est nécessaire ici (lib/manager.sh, lib/nodes.sh, lib/models.sh,
# ou n'importe quel autre fichier du dépôt hors de la liste "heavy") ne
# réinvalide donc que ce script, jamais les étapes coûteuses ci-dessus.
#
# Suppose que docker-build-steps-heavy.sh a déjà tourné dans un layer
# antérieur (venv créé, PyTorch préchargé, ComfyUI cloné) : ce script
# n'a besoin de RIEN refaire de tout cela, seulement de le réutiliser.

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
source "${PROJECT_ROOT}/lib/python.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/manager.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/nodes.sh"
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/lib/models.sh"

log_step "Construction de l'image Docker pré-installée — étapes bon marché (sans GPU)"

# Même raison que docker-build-steps-heavy.sh : aucun GPU visible ici, donc
# pip_install_requirements() (lib/python.sh, utilisée par
# install_or_update_manager/install_optional_nodes) doit continuer à filtrer
# torch/torchvision/torchaudio de tout requirements.txt tiers.
export DOCKER_BUILD_NO_TORCH=true

install_or_update_manager
install_optional_nodes
create_model_folders

# Marque ces étapes comme faites dans le state file — voir le commentaire
# équivalent dans docker-build-steps-heavy.sh pour le principe complet
# (state file cumulatif, réutilisé par install.sh au démarrage du
# conteneur).
for step in manager_installed optional_nodes model_folders; do
  mark_step_done "$step"
done

# --- Nettoyage (même raison que docker-build-steps-heavy.sh) ---------------
# Ce script est sa PROPRE couche Docker (COPY . puis RUN ./docker-build-steps-light.sh
# dans le Dockerfile) : install_or_update_manager/install_optional_nodes
# font aussi des pip install (requirements.txt de ComfyUI-Manager et des
# nœuds custom optionnels) — PIP_NO_CACHE_DIR (Dockerfile) couvre déjà
# l'essentiel, ceci est un filet de sécurité, dans la même couche.
log_step "Nettoyage de la couche Docker (caches résiduels)"
rm -rf /root/.cache/pip /root/.cache/* /tmp/* 2>/dev/null || true
find "${VENV_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

log_ok "Image Docker pré-installée : étapes bon marché terminées (nettoyée)."
