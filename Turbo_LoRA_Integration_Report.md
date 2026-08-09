# Turbo LoRA Integration Report

Date : 2026-08-09

## Custom Node

- **Dépôt** : `https://github.com/larryvrh/ComfyUI-MiniMax-H3-Turbo.git` (auteur `Larryvrh`, vérifié en ligne avant modification)
- **Emplacement final** : `/workspace/ComfyUI/custom_nodes/ComfyUI-MiniMax-H3-Turbo/`
- **Nodes fournis** (d'après le README officiel du dépôt) :
  - `MiniMax-H3 Turbo LoRA` — `MODEL → MODEL`, applique le Turbo LoRA
  - `MiniMax-H3 Turbo Sampler (4-step)` — alimente `SamplerCustomAdvanced`
- **Dépendances** : aucune. Le dépôt ne contient pas de `requirements.txt` (fichiers présents : `.gitignore`, `LICENSE`, `README.md`, `__init__.py`, `pyproject.toml`) — aucun `pip install` déclenché.
- **Méthode d'installation** : `git clone` simple dans `custom_nodes/`, exactement la méthode manuelle documentée par le dépôt. Licence Apache-2.0.
- **Idempotence** : installation via une fonction dédiée `install_turbo_node()` (`lib/lora_auto.sh`), **volontairement séparée** de `install_optional_nodes()` (`lib/nodes.sh`) car ce dernier fait un `git pull --ff-only` à chaque exécution sur tout dépôt déjà cloné — comportement explicitement refusé pour ce node. `install_turbo_node()` : absent → clone ; présent → aucune action, jamais de pull automatique.

## LoRA

- **Source retenue** : Hugging Face, `drbaph/MiniMax-H3-Turbo-Lora-ComfyUI` — conversion tierce du LoRA original de `larryvrh`, spécifiquement rendue compatible avec le checkpoint H3 **pruned/curve-form** (celui utilisé par ce projet : `minimax_h3_ref2va_pruned_int8_convrot.safetensors`). Le LoRA original (`larryvrh/MiniMax-H3-Turbo-Lora`) ne charge pas sur un checkpoint pruned (couches AdaLN incompatibles) — point vérifié avant modification et validé avec toi en amont.
- **URL exacte** :
  `https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI/resolve/main/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors`
  (fichier confirmé présent dans le dépôt, listé comme "Recommended — v4 Step-600 EMA" par son auteur)
- **Nom local** : `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors` (identique au nom distant ici, mais désormais **imposé explicitement** via `--filename`, indépendamment de l'URL)
- **Emplacement final** : `/workspace/ComfyUI/models/loras/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors`
- **Mécanisme de renommage** : nouveau flag `--filename <nom>` dans `install_lora.sh` (voir plus bas). Priorité absolue sur la résolution Content-Disposition/URL existante, qui reste le comportement par défaut si `--filename` est omis.
- **Idempotence** : test `[[ -s "$dest_file" ]]` sur le nom final (explicite ou résolu) — inchangé, juste appliqué au bon nom désormais. Avec un nom explicite, **aucune requête réseau** n'est nécessaire pour vérifier la présence du fichier (testé : TEST 4).

## Configuration (`config.env`)

Nouvelles variables, toutes avec valeur par défaut (rien à faire si tu ne veux rien changer) :

| Variable | Valeur par défaut |
|---|---|
| `MINIMAX_H3_TURBO_LORA_AUTO_DOWNLOAD` | `true` (inchangé) |
| `MINIMAX_H3_TURBO_LORA_URL` | URL Hugging Face ci-dessus |
| `MINIMAX_H3_TURBO_LORA_FILENAME` | `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors` (**nouveau**) |
| `MINIMAX_H3_TURBO_NODE_AUTO_INSTALL` | `true` (**nouveau**) |
| `MINIMAX_H3_TURBO_NODE_REPO` | `https://github.com/larryvrh/ComfyUI-MiniMax-H3-Turbo.git` (**nouveau**) |

Chaque variable n'est définie qu'à un seul endroit (`config.env`), aucune duplication ailleurs dans le projet.

## Fichiers modifiés

- `config.env` — section Turbo LoRA réécrite (source CivitAI → Hugging Face + nom explicite), nouvelle section custom node Turbo.
- `install_lora.sh` — ajout du flag `--filename <nom>` (usage, aide, `determine_lora_filename()`, `install_lora()`, parsing dans `main()`). Aucun appel existant sans `--filename` n'est affecté.
- `lib/lora_auto.sh` — `install_turbo_lora()` transmet `--filename` quand `MINIMAX_H3_TURBO_LORA_FILENAME` est défini ; nouvelle fonction `install_turbo_node()`.
- `install.sh` — appel de `install_turbo_node()` juste avant chaque appel existant à `install_turbo_lora` (branche `--only-models` et flux principal).
- `update.sh` — appel de `install_turbo_node()` juste avant `install_turbo_lora` (nécessaire car `bootstrap.sh` route tout pod déjà installé vers `update.sh`, jamais `install.sh`).

## Fichiers ajoutés

- `Turbo_LoRA_Integration_Report.md` (ce fichier)

## Fichiers volontairement inchangés

- `workflows/*.json` — **strictement inchangés**, voir vérification ci-dessous.
- `lib/workflows.sh` — non touché.
- `lib/nodes.sh` — non touché (sa logique `git pull` existante reste valable pour `OPTIONAL_NODE_REPOS`/`OPTIONAL_NODE_REPOS_NO_PIP`, simplement non réutilisée pour ce node Turbo).
- SageAttention, Triton, CUDA, PyTorch, flags de lancement ComfyUI, autres custom nodes, modèles H3, VAE, text encoder — aucun n'a été touché.
- Comportement historique de `install_lora.sh` sans `--filename` — inchangé (vérifié : aucun paramètre nouveau n'est requis pour les appels existants).

## Dépendances installées

Aucune nouvelle dépendance Python. Le node Turbo n'en nécessite aucune (pas de `requirements.txt`).

## Tests réalisés

Exécutés dans un environnement `/tmp/fake_comfy` isolé (réseau désactivé dans ce bac à sable — les tentatives de téléchargement/clone échouent donc *nécessairement*, ce qui permet justement de valider le comportement non bloquant).

| # | Scénario | Résultat attendu | Résultat obtenu | Statut |
|---|---|---|---|---|
| 1 | Custom node absent | Tentative de clone | Clone tenté, échec réseau absorbé en warning, script non interrompu | PASS |
| 2 | Custom node déjà présent | Aucun clone, aucun `git pull` | Confirmé par un espion `git` : **zéro invocation de `git`** | PASS |
| 3 | LoRA absent | Tentative de téléchargement | Téléchargement tenté (échec réseau attendu dans ce bac à sable) | PASS |
| 4 | LoRA déjà présent (nom explicite) | Aucun téléchargement, fichier non réécrit | Confirmé par un espion `curl` : **zéro invocation de `curl`** ; mtime et SHA256 identiques avant/après | PASS |
| 5 | Nom du fichier final | `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors` exactement | Confirmé | PASS |
| 6 | Emplacement final | `/workspace/ComfyUI/models/loras/...` (`.../models/loras/` dans le bac à sable) | Confirmé | PASS |
| 7 | `MiniMaxH3TurboLoRA` disponible | Node chargé par ComfyUI | **Non vérifiable ici** — nécessite un ComfyUI réellement démarré, absent de ce bac à sable. Nodes documentés par le README du dépôt (`MiniMax-H3 Turbo LoRA`), noms `class_type` internes non confirmés programmatiquement. | À vérifier après déploiement réel |
| 8 | `MiniMaxH3TurboSampler` disponible | Idem | Idem TEST 7 | À vérifier après déploiement réel |
| 9 | Workflows JSON inchangés | Checksums identiques avant/après | `sha256sum workflows/*.json` strictement identique avant/après (voir ci-dessous) | PASS |
| 10 | Deuxième exécution (relance) | Custom node → skip, LoRA → skip | Voir TEST 2 et TEST 4, tous deux exécutés sur un état "déjà présent" | PASS |
| 11 | Échec réseau (LoRA) | Warning, installation globale non bloquée | Confirmé (TEST 3), le script continue après l'échec | PASS |

### Checksums des workflows (avant = après)

```
4a5e253f45f193ebb6a4df8206534b3ff97f080e4e1f44f5ecdd3cd30431b13e  workflows/MiniMax_H3_REF2V_TURBO_PLUS_SAGE.json
bb71aecdd3c0b62e56eafe03acb14d1cfeabec7072eaed9cbdf473c2aaf73009  workflows/video_minimax_h3_i2v.json
099d24eda6263854818975c7209db6f29ebfd0339936c928f12293d5ab029ffb  workflows/video_minimax_h3_r2v.json
31ab33fdb053a7834cc866bd7aa08b887518fc656e4a796c89779c6b5e1786e6  workflows/video_minimax_h3_t2v.json
```
Identiques bit pour bit avant et après toutes les modifications de cette tâche.

### Non-régression

- `bash -n` : PASS sur `install.sh`, `update.sh`, `config.env`, `install_lora.sh`, `lib/lora_auto.sh`.
- `set -Eeuo pipefail` : respecté partout ; aucun appel réseau/`git`/`bash install_lora.sh` n'est fait sans gestion explicite du code de sortie (`if !`), donc aucun risque d'arrêt intempestif du script appelant.
- Ancienne URL CivitAI (`3202732`/`3091422`) : **absente** de tout le projet (grep vérifié) — seules les mentions génériques de CivitAI (mécanisme réutilisable pour d'autres LoRA, et `H3_CIVITAI_FL2VA/REF2VA_URL` pour les checkpoints H3, hors périmètre) subsistent.
- Une seule source de vérité pour chaque variable Turbo (`grep -c` confirmé = 1 occurrence de définition par variable dans `config.env`).

## Point restant à vérifier après déploiement réel

Les TEST 7/8/9 (disponibilité effective des nodes dans l'interface ComfyUI, `class_type` exacts) nécessitent un lancement réel de ComfyUI avec le node installé — impossible à simuler sans réseau ni environnement ComfyUI complet. À vérifier au premier lancement réel sur le pod : ouvrir l'interface, chercher `MiniMax-H3 Turbo LoRA` / `MiniMax-H3 Turbo Sampler` dans le menu d'ajout de node, et confirmer que ton workflow existant (qui référence déjà ces nodes) se charge sans erreur "node manquant".
