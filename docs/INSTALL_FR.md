# Guide d'installation

Ce guide explique comment déployer **ComfyUI + MiniMax H3** sur un RunPod
neuf, étape par étape. Pour la version courte, voir le
[README.md](../README.md#-quick-start) (en anglais).

---

# Prérequis

Avant de commencer, vous devez disposer de :

- un compte RunPod ;
- un compte Hugging Face, avec la licence MiniMax H3 acceptée sur
  <https://huggingface.co/Comfy-Org/MiniMax-H3>, et un token d'accès
  Hugging Face avec le droit **Read**.

---

# GPU et choix du palier

Tout GPU NVIDIA avec **8 Go de VRAM ou plus** convient. L'installateur
télécharge l'un des trois paliers de poids MiniMax H3, dimensionnés selon
la VRAM disponible :

| VRAM du GPU | Palier | Remarques |
|---|---|---|
| 48 Go+ | `max` | Qualité maximale (poids BF16). **C'est le palier par défaut du projet**, quel que soit votre GPU réel. |
| 24-47 Go | `balanced` | Poids INT8 ConvRot pruned, environ trois fois plus légers que `max`. |
| 8-23 Go | `light` | Poids INT4Q mixtes INT4/INT8 (dépôt Hugging Face séparé), les plus légers et les plus rapides à télécharger. |

Le palier par défaut (`max`) ne s'adapte pas automatiquement à un GPU plus
modeste — décidez donc en amont :

- Sur une carte 48 Go+, les valeurs par défaut conviennent : lancez
  simplement `bootstrap.sh`.
- Sur une carte plus petite, indiquez explicitement `--tier=light` ou
  `--tier=balanced`, ou passez `--tier=auto` pour laisser l'installateur
  choisir un palier selon la VRAM détectée (voir
  [Étape 3](#étape-3--optionnel--choisir-un-palier-ou-un-sous-ensemble-de-workflows)
  ci-dessous).

Voir [README.md § Model tiers](../README.md#-model-tiers-h3_tier) pour les
tailles exactes (en anglais).

---

# Étape 1 — Créer un RunPod

Créez un nouveau Pod.

Template recommandé :

- PyTorch
- CUDA 12.x (l'installateur détecte et adapte automatiquement la version
  CUDA réellement annoncée par le pilote de votre pod — voir
  [FAQ § Which CUDA and PyTorch build gets installed?](../FAQ.md#which-cuda-and-pytorch-build-gets-installed))
- Ubuntu

Exposez le port `8188` en HTTP.

---

# Étape 2 — Ouvrir le terminal et cloner l'installateur

```bash
cd /workspace
git clone https://github.com/Kinderheim512/minimax-runpod-installer.git
cd minimax-runpod-installer
```

---

# Étape 3 — (Optionnel) choisir un palier ou un sous-ensemble de workflows

Passez cette étape pour utiliser les valeurs par défaut (palier `max`,
les trois workflows). Pour personnaliser, éditez `config.env` ou passez
directement des options :

```bash
# Choisir automatiquement un palier selon la VRAM détectée
bash bootstrap.sh --tier=auto

# Ou forcer un palier précis
bash bootstrap.sh --tier=light

# N'installer que pour Text-to-Video et Image-to-Video (sans REF2VA)
bash bootstrap.sh --workflows=t2v,i2v
```

`bootstrap.sh` transmet ses arguments à `install.sh` lors du premier
lancement. Voir [README.md § CLI reference](../README.md#-cli-reference)
pour la liste complète des options (en anglais).

---

# Étape 4 — Lancer l'installateur

```bash
bash bootstrap.sh
```

Ceci effectue automatiquement :

- la détection du GPU et le choix d'une version PyTorch/CUDA adaptée ;
- la création de l'environnement virtuel Python ;
- l'installation de ComfyUI, de ComfyUI-Manager et des nœuds optionnels
  (VideoHelperSuite, Spectrum MiniMax H3) ;
- la création de tous les dossiers de modèles requis ;
- l'estimation de l'espace disque requis, puis le téléchargement du palier
  et des workflows MiniMax H3 sélectionnés (ou des valeurs par défaut) ;
- l'installation des workflows officiels correspondants ;
- le calcul des paramètres de lancement adaptés au GPU ;
- le lancement de ComfyUI **dans une session tmux persistante** — voir
  [TMUX.md](../TMUX.md) (en anglais) pour comprendre ce que cela implique
  et comment s'y rattacher plus tard.

Aucune configuration manuelle n'est nécessaire au-delà de ce que vous avez
choisi à l'étape 3.

---

# Étape 5 — Authentification Hugging Face

Si demandé, saisissez votre token d'accès Hugging Face (ou définissez
`HF_TOKEN` en variable d'environnement / secret RunPod au préalable pour
éviter la question). L'installateur vérifie que vous avez bien accès au
dépôt MiniMax H3 (soumis à licence) avant de télécharger quoi que ce soit,
et indique précisément la marche à suivre si la licence n'a pas encore été
acceptée.

---

# Étape 6 — Ouvrir ComfyUI

L'adresse de votre Pod ressemble à :

```
https://VOTRE-POD-ID-8188.proxy.runpod.net
```

---

# Workflows inclus

L'installateur installe les fichiers de workflow officiels correspondant à
votre sélection `--workflows` (les trois tâches par défaut) — voir
[README.md § Workflows](../README.md#-workflows) pour la liste complète des
5 fichiers et leur contenu (en anglais). Aucune importation manuelle : ils
apparaissent directement dans ComfyUI.

> Si le nœud de chargement de modèle d'un workflow affiche un fichier
> différent de celui que vous avez téléchargé, il s'agit d'une
> incohérence de nommage connue entre les workflows officiels et les
> paliers, pas d'un modèle manquant — voir
> [TROUBLESHOOTING.md](../TROUBLESHOOTING.md#a-workflow-says-a-model-is-missing-even-though-checksh-says-its-installed)
> (en anglais).

---

# Installer un LoRA

```bash
bash install_lora.sh "https://civitai.red/api/download/models/XXXX?fileId=XXXX"
bash install_lora.sh --list
```

Installé dans `ComfyUI/models/loras/`. Voir
[README.md § Installing and managing LoRAs](../README.md#-installing-and-managing-loras)
et [RECOMMENDED_LORAS.md](../RECOMMENDED_LORAS.md) (en anglais).

---

# Mise à jour

```bash
git pull
bash install.sh
```

Seuls les composants manquants ou obsolètes sont mis à jour. Les modèles
déjà téléchargés ne sont jamais retéléchargés.

---

# Vérifier l'installation

```bash
bash check.sh
```

Vérifie le GPU, CUDA/PyTorch, les modèles MiniMax H3 requis par votre
sélection de palier/workflows actuelle, ComfyUI-Manager, Spectrum, et
l'espace disque libre — sans rien modifier.

---

# Dépannage

Regroupé sur une seule page dédiée pour rester cohérent partout :
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md) (en anglais). Premiers réflexes
courants :

- Erreur CUDA Out Of Memory → réduisez résolution/frames/steps, ou utilisez
  un palier plus léger ;
- Erreur Hugging Face → vérifiez que la licence est acceptée et que le
  token est valide ;
- Un workflow signale un modèle manquant → il s'agit presque toujours d'une
  incohérence de nommage entre paliers, pas d'un fichier réellement absent.

---

# Support

Si ce projet vous est utile, n'hésitez pas à lui attribuer une ⭐ sur
GitHub.
