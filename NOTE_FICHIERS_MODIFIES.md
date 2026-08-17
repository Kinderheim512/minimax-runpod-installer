# Récapitulatif des changements de cette session

## Fichier supprimé
- `docker-build-steps.sh` → remplacé par les deux fichiers ci-dessous.
  Pense à le supprimer toi-même de ton dépôt, ce zip ne contient que des
  fichiers à ajouter/écraser, pas d'instruction de suppression.

## Fichiers nouveaux
- `docker-build-steps-heavy.sh` (étapes coûteuses : apt, clone ComfyUI,
  venv/dépendances, PyTorch, wheel SageAttention)
- `docker-build-steps-light.sh` (étapes bon marché : ComfyUI-Manager,
  nœuds custom, dossiers modèles)

## Fichiers modifiés
- `Dockerfile` — COPY en deux temps pour profiter du cache de layers Docker
- `.github/workflows/docker-build.yml` — `paths-ignore` ajouté (doc-only
  commits ne déclenchent plus le build)
- `lib/python.sh` — fix SageAttention (comparaison nvcc/torch limitée à la
  branche CUDA majeure au lieu de la mineure exacte) + mise à jour des
  références à l'ancien nom de script
- `CHANGELOG.md` — deux nouvelles entrées sous `[Unreleased]`
- `README.md`, `TROUBLESHOOTING.md`, `docker-entrypoint.sh`,
  `lib/personal_storage.sh` — mise à jour des références à l'ancien nom de
  script (`docker-build-steps.sh` → `docker-build-steps-heavy.sh` /
  `docker-build-steps-light.sh`), aucun changement fonctionnel
