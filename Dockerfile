# Dockerfile — image Docker pré-installée pour l'installeur MiniMax H3 /
# RunPod (ComfyUI + venv + dépendances Python, SAUF PyTorch et les poids H3).
#
# Objectif (voir la section "Image Docker pré-installée" du README pour le
# contexte complet) : permettre de terminate un pod RunPod sans arrière-
# pensée et repartir sur n'importe quel autre pod/datacenter/provider avec
# un GPU disponible, sans tout retélécharger à chaque fois (clone ComfyUI,
# venv, dépendances) — seuls PyTorch (quelques secondes/minutes, dépend du
# GPU réellement obtenu) et les poids H3 (Hugging Face, gratuits, mais
# forcément après coup puisqu'ils dépendent du choix de palier) restent à
# récupérer à chaque nouveau conteneur.
#
# Ce mode est un COMPLÉMENT à install.sh (usage bash classique sur pod nu),
# jamais un remplacement : install.sh continue de fonctionner seul, à
# l'identique, pour qui ne veut pas utiliser cette image.
#
# Rien dans cette image ne dépend du GPU sur lequel elle finira par
# tourner, à UNE exception assumée près : PyTorch (voir plus bas,
# PREFER_CUDA130) est pré-installé avec le build le plus récent connu
# (cu130) — un pari, pas une détection, vérifié et corrigé automatiquement
# au démarrage du conteneur si le pilote du pod ne le supporte pas. PAS de
# poids H3 en revanche (plusieurs dizaines de Go, et le palier dépend de la
# VRAM du GPU obtenu — resterait de toute façon spécifique à l'usage). Une
# seule image sert donc à (quasi) tous les GPU RunPod (T4 comme H100),
# PyTorch inclus dans la majorité des cas.
#
# Base Ubuntu simple (pas nvidia/cuda) : les wheels PyTorch embarquent déjà
# leur propre runtime CUDA — un toolkit CUDA système complet n'est
# nécessaire que pour la compilation optionnelle de SageAttention, et
# install_sageattention() (lib/python.sh) sait déjà l'installer à la
# demande, au démarrage, avec la version exacte attendue par le torch
# réellement installé (voir ce fichier pour le détail).
FROM ubuntu:22.04

# Pas de prompts apt interactifs pendant le build.
ENV DEBIAN_FRONTEND=noninteractive

# Locale UTF-8 — sans ça, les caractères accentués (français, dans tous les
# logs de ce projet) s'affichent corrompus dans le terminal (un "_" à la
# place de chaque caractère type é/à/ç), y compris à l'intérieur d'une
# session tmux : Ubuntu ne configure aucune locale par défaut dans une image
# minimale. C.UTF-8 est toujours disponible sans paquet supplémentaire
# (contrairement à en_US.UTF-8, qui demanderait `locale-gen` + le paquet
# `locales`) — inutile d'alourdir l'image pour ça.
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# INSTALL_DIR dédié à l'image (différent du défaut /workspace/ComfyUI utilisé
# par install.sh sur pod nu) : sur RunPod, un Network Volume attaché est
# monté sur /workspace et EN ÉCRASERAIT le contenu pré-installé par cette
# image (le montage a lieu après le démarrage du conteneur, par-dessus tout
# ce que l'image contenait à ce chemin) — /opt/ComfyUI n'est jamais un point
# de montage RunPod, donc jamais recouvert. Si vous utilisez tout de même un
# Network Volume avec cette image, montez-le ailleurs (ex: pour le stockage
# perso, voir PERSONAL_STORAGE_HF_REPO — pensé justement pour ne plus avoir
# besoin d'un Network Volume).
ENV INSTALL_DIR=/opt/ComfyUI
ENV PROJECT_ROOT=/opt/minimax-runpod-installer

# PyTorch (build le plus récent connu, actuellement cu130) est pré-installé
# dans cette image — voir docker-build-steps-heavy.sh::bake_pytorch_best_guess()
# pour le détail. PREFER_CUDA130=true active, par défaut UNIQUEMENT dans
# cette image (jamais dans config.env, jamais pour install.sh/update.sh sur
# pod nu), le mécanisme de vérification-avec-repli déjà en place
# (lib/python.sh) : au démarrage du conteneur, ce build préchargé est
# réutilisé tel quel si le pilote GPU du pod le permet (démarrage quasi
# instantané), ou remplacé automatiquement par le build compatible sinon —
# jamais de pod cassé. Peut être désactivée en surchargeant cette variable
# d'environnement au niveau du pod (PREFER_CUDA130=false) pour revenir à la
# détection stricte habituelle.
ENV PREFER_CUDA130=true

WORKDIR ${PROJECT_ROOT}

# --- Étape 1/2 : fichiers dont dépendent les étapes COÛTEUSES uniquement ---
# (apt, clone ComfyUI, venv/dépendances, PyTorch, wheel SageAttention — voir
# docker-build-steps-heavy.sh pour le détail et le raisonnement complet du
# découpage). Grâce au cache de layers Docker (content-based avec
# Buildx/BuildKit, cf. .github/workflows/docker-build.yml), tant qu'aucun de
# CES fichiers précis ne change, ce COPY et le RUN qui suit sont réutilisés
# tels quels — même si le commit qui déclenche le build modifie n'importe
# quel autre fichier du dépôt (README, docs, presets, workflows, ou même
# lib/manager.sh / lib/nodes.sh / lib/models.sh). C'est le levier principal
# pour ne pas repayer apt+CUDA+PyTorch+SageAttention (de loin les étapes les
# plus longues) à chaque commit — voir aussi le filtre `paths-ignore` du
# workflow, qui évite même de déclencher le build pour les commits qui ne
# touchent à rien de pertinent pour l'image.
COPY config.env requirements.txt docker-build-steps-heavy.sh ${PROJECT_ROOT}/
COPY lib/utils.sh lib/system.sh lib/comfyui.sh lib/python.sh lib/i18n.sh ${PROJECT_ROOT}/lib/
COPY lib/lang/ ${PROJECT_ROOT}/lib/lang/

# Conversion CRLF -> LF (même geste que install.sh en tout début d'exécution,
# utile si l'image est construite depuis un clone Windows) + droits d'exec —
# limité à ce qui vient d'être copié, pas au reste du dépôt (pas encore
# présent à ce stade).
RUN find "${PROJECT_ROOT}" -name "*.sh" -exec sed -i 's/\r$//' {} \; \
    && find "${PROJECT_ROOT}" -name "*.sh" -exec chmod +x {} \;

# SageAttention n'est plus pré-compilée à la construction de l'image (voir
# docker-build-steps-heavy.sh : l'appel à bake_sageattention_wheel() a été
# retiré — le toolkit CUDA complet qu'il installait en plus faisait planter
# le build par manque d'espace disque). install_sageattention()
# (lib/python.sh) continue de fonctionner normalement au démarrage d'un
# conteneur si SAGE_ATTENTION=true/auto est activé : elle compile alors
# depuis les sources à ce moment-là, comme sur un pod nu sans image Docker.
RUN ./docker-build-steps-heavy.sh

# --- Étape 2/2 : reste du dépôt, pour les étapes bon marché uniquement ----
# docker-build-steps-light.sh a besoin du reste du dépôt (presets,
# workflows, lib/manager.sh, lib/nodes.sh, lib/models.sh...) — voir le
# commentaire d'en-tête de chaque docker-build-steps-*.sh sur le principe
# "ne jamais dupliquer la logique existante". Ce second COPY invalide bien
# le cache à partir d'ici à CHAQUE commit qui touche un seul de ces
# fichiers (presets, workflows, README, lib/manager.sh...), mais ce qui
# suit (docker-build-steps-light.sh) est volontairement rapide — c'est
# l'étape 1/2 ci-dessus qui protège le gros du temps de build.
COPY . ${PROJECT_ROOT}

RUN find "${PROJECT_ROOT}" -name "*.sh" -exec sed -i 's/\r$//' {} \; \
    && find "${PROJECT_ROOT}" -name "*.sh" -exec chmod +x {} \;

RUN ./docker-build-steps-light.sh

ENTRYPOINT ["/opt/minimax-runpod-installer/docker-entrypoint.sh"]
