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
# dans cette image — voir docker-build-steps.sh::bake_pytorch_best_guess()
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

# Le repo entier est copié tel quel : docker-build-steps.sh a besoin de
# config.env et de tout lib/*.sh (résolution de version ComfyUI, table
# PyTorch pour référence future, listes de paquets/nœuds...) exactement comme
# install.sh — voir le commentaire d'en-tête de docker-build-steps.sh sur le
# principe "ne jamais dupliquer la logique existante".
COPY . ${PROJECT_ROOT}

# Conversion CRLF -> LF (même geste que install.sh en tout début d'exécution,
# utile si l'image est construite depuis un clone Windows) + droits d'exec.
RUN find "${PROJECT_ROOT}" -name "*.sh" -exec sed -i 's/\r$//' {} \; \
    && find "${PROJECT_ROOT}" -name "*.sh" -exec chmod +x {} \;

# Étapes sans GPU (système, ComfyUI, venv, dépendances hors PyTorch, nœuds
# custom) — voir docker-build-steps.sh, qui réutilise lib/*.sh à l'identique
# de install.sh plutôt que de dupliquer la moindre commande git/pip ici.
RUN ./docker-build-steps.sh

ENTRYPOINT ["/opt/minimax-runpod-installer/docker-entrypoint.sh"]
