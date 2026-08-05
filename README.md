# Installateur automatique MiniMax H3 pour RunPod + ComfyUI

Déploie automatiquement [ComfyUI](https://github.com/comfyanonymous/ComfyUI) et
[MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3) (modèle
texte/image/vidéo/audio → vidéo avec audio stéréo natif, poids ouverts publiés
début août 2026) sur un pod RunPod, en une commande.

## ⚠️ À savoir avant de lancer

**MiniMax H3 est supporté nativement par ComfyUI depuis la version 0.30.0**
(nœuds `MiniMaxH3ImageToVideo` et `MiniMaxH3ReferenceToVideo` inclus dans le
cœur du logiciel). **Aucun custom node n'est requis** pour le faire tourner —
contrairement à beaucoup de modèles vidéo plus anciens. Ce projet installe
donc ComfyUI-Manager (pour la gestion future de nœuds) et un petit paquet
optionnel (ComfyUI-VideoHelperSuite) purement pour le confort, mais rien de
tout cela n'est nécessaire au fonctionnement de H3 en lui-même.

Les poids sont hébergés sur un dépôt Hugging Face **soumis à licence**
(`minimax-h3-community-license-agreement`). Vous devez :
1. avoir un compte Hugging Face,
2. accepter la licence sur https://huggingface.co/Comfy-Org/MiniMax-H3,
3. fournir un token d'accès (lecture suffit) lors de l'installation.

Ce projet n'essaie jamais de contourner cette étape : sans licence acceptée,
le téléchargement des modèles échoue proprement avec un message explicite.

## Installation en une commande

```bash
bash install.sh
```

Le script installe les paquets système, crée un environnement virtuel Python,
clone/installe ComfyUI et ComfyUI-Manager, prépare l'arborescence de modèles,
demande votre token Hugging Face si besoin, télécharge les poids MiniMax H3
adaptés à votre GPU, calcule les meilleurs réglages de lancement, puis
affiche un résumé.

Options utiles :

```bash
bash install.sh --skip-models          # installe tout sauf les poids (à faire plus tard)
bash install.sh --only-models          # (re)télécharge uniquement les poids
bash install.sh --tier=light           # force le palier léger (int8 pruned + nvfp4)
bash install.sh --workflows=t2v,r2v    # ne prépare que ces workflows
bash install.sh --yes                  # non interactif
bash install.sh --force                # réexécute toutes les étapes, même déjà validées
```

Le script est **idempotent et reprenable** : s'il est interrompu (coupure
réseau, timeout RunPod...), relancez simplement `bash install.sh` — les
étapes déjà terminées sont sautées automatiquement (état suivi dans
`minimax-runpod-installer/.minimax_installer_state`, à côté de `install.sh`,
pour ne jamais interférer avec le clonage de ComfyUI dans `INSTALL_DIR`).

## Paliers de poids

| Palier     | Diffusion (fl2va/ref2va) | Encodeur texte (Qwen3-VL 32B) | ~Go / workflow | GPU visé                     |
|------------|---------------------------|--------------------------------|----------------|-------------------------------|
| `light`    | pruned_int8_convrot (21 Go)| nvfp4_awq (15,7 Go)            | ~40 Go         | 8–24 Go VRAM (ex. RTX 3060, avec offload) |
| `balanced` | int8_convrot (34 Go)       | int8_convrot (27,1 Go)         | ~65 Go         | 24–48 Go VRAM (RTX 4090, A100 40, L40S)   |
| `max`      | bf16 (66,3 Go)              | bf16 (51,5 Go)                  | ~130 Go        | ≥ 48 Go VRAM (A100 80, H100, H200)        |

Par défaut (`H3_TIER=auto` dans `config.env`), le palier est choisi
automatiquement d'après la VRAM détectée. Deux VAE (vidéo + audio, quelques
Go au total) sont toujours téléchargés en plus, quel que soit le palier.

Les workflows T2V et I2V partagent le même modèle de diffusion (`fl2va`) ;
R2V (référence → vidéo) utilise un jeu de poids différent (`ref2va`). Par
défaut seuls T2V et I2V sont préparés (`H3_WORKFLOWS="t2v,i2v"` dans
`config.env`) ; ajoutez `r2v` ou utilisez `all` si vous voulez aussi la
génération pilotée par références.

## Structure du projet

```
minimax-runpod-installer/
├── install.sh       installation complète (une commande)
├── update.sh         mise à jour ComfyUI / Manager / nœuds / deps
├── launch.sh          lance ComfyUI avec les optimisations GPU calculées
├── menu.sh             menu interactif (1-7)
├── check.sh             vérification sans rien modifier
├── uninstall.sh           désinstallation propre (avec confirmations)
├── config.env               configuration centrale
├── requirements.txt           dépendances Python additionnelles du projet
├── lib/
│   ├── utils.sh                logging, erreurs, confirmation, état/reprise
│   ├── system.sh                 paquets système (git, aria2, ffmpeg...)
│   ├── gpu.sh                      détection GPU/VRAM/CUDA, choix du palier
│   ├── python.sh                     venv, dépendances Python
│   ├── comfyui.sh                      clone/mise à jour de ComfyUI
│   ├── manager.sh                        ComfyUI-Manager
│   ├── nodes.sh                            nœuds custom optionnels
│   ├── huggingface.sh                        login HF, vérif accès licence
│   ├── download.sh                             téléchargement + vérification
│   ├── models.sh                                 manifeste des poids H3
│   ├── optimization.sh                             réglages selon le GPU
│   └── verify.sh                                     vérifications, résumé
└── logs/
    ├── install.log
    ├── update.log
    └── download.log (fusionné dans install.log par défaut)
```

## Menu interactif

```bash
bash menu.sh
```

```
1) Installer
2) Télécharger les modèles
3) Vérifier l'installation
4) Mettre à jour
5) Lancer ComfyUI
6) Désinstaller
7) Quitter
```

## Lancement

```bash
./launch.sh
```

Démarre `python main.py --listen 0.0.0.0 --port 8188` avec les flags calculés
pour votre GPU (`--highvram`/`--lowvram`/`--reserve-vram`, `--fast` sur
Ampere/Ada/Hopper, backend d'attention adapté). Sur RunPod, si la variable
`RUNPOD_POD_ID` est présente, l'URL du proxy HTTP est affichée automatiquement
(`https://<pod-id>-8188.proxy.runpod.net`) — pensez à exposer le port 8188 en
HTTP dans les réglages du pod.

## Vérification

```bash
bash check.sh
```

Contrôle GPU, CUDA/PyTorch, ComfyUI, ComfyUI-Manager, présence et taille des
modèles H3, espace disque libre — sans rien modifier.

## Mise à jour

```bash
bash update.sh
```

Met à jour ComfyUI, ComfyUI-Manager, les nœuds optionnels et les dépendances
Python. Ne re-télécharge pas les modèles déjà présents et valides.

## Désinstallation

```bash
bash uninstall.sh
```

Supprime ComfyUI, le venv, ComfyUI-Manager et les nœuds custom. Demande une
confirmation séparée avant de supprimer les modèles (potentiellement des
dizaines de Go). Ne touche jamais aux paquets système installés par
`lib/system.sh` (git, aria2, ffmpeg...).

## Logs

Chaque script écrit dans `logs/` (`install.log`, `update.log`, `launch.log`,
`uninstall.log`) avec horodatage implicite par écriture au fil de l'eau —
consultez-les en cas d'échec, le message d'erreur y renvoie systématiquement.

## Sources

- ComfyUI : https://github.com/comfyanonymous/ComfyUI
- ComfyUI-Manager : https://github.com/ltdrdata/ComfyUI-Manager
- MiniMax H3 (poids officiels repackagés ComfyUI) : https://huggingface.co/Comfy-Org/MiniMax-H3
- MiniMax H3 (dépôt original MiniMaxAI) : https://huggingface.co/MiniMaxAI/MiniMax-H3
- Documentation d'usage ComfyUI : https://docs.comfy.org/tutorials/video/minimax/minimax-h3
- Annonce technique (paliers de quantification, VRAM) : https://blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui
