# DaSiWa Preset — Redesign (mode classic / director)

Refonte du preset `dasiwa_mmh3v12` : un nouveau paramètre de manifeste
`H3_DASIWA_MODE` (défaut `classic`, non cassant) introduit deux modes, avec
4 chemins vitesse **mutuellement exclusifs** et des garde-fous anti-empilement.

## 1. Modes

| Variable | Valeurs | Défaut | Effet |
|---|---|---|---|
| `H3_DASIWA_MODE` | `classic` / `director` | `classic` | Choix du workflow & des dépendances |

- **classic (Option A)** — workflow MythicAlchemy (chargeur checkpoint),
  comportement historique strictement inchangé pour une installation
  existante. `H3_DASIWA_CHECKPOINT_VARIANTS` (CSV) choisit le(s)
  checkpoint(s).
- **director (Option B)** — mode Director (`MiniMaxH3Director` /
  `MiniMaxH3DirectorGuide`), workflow **C-MMH3-18 épinglé** (fichier local
  `presets/dasiwa_mmh3v12/director/C-MMH3-18.json`), checkpoint hybride
  (`H3_DASIWA_DIRECTOR_HYBRID_VARIANT`), **VAE vidéo int8 Kijai + pack LBH
  installés automatiquement** (non optionnels).

## 2. Quatre chemins vitesse — mutuellement exclusifs

| Chemin (`H3_DASIWA_SPEED_PATH`) | Checkpoint | Accélérateur | Steps |
|---|---|---|---|
| `slow` (défaut) | officiel pruned | — | ~25 |
| `turbo_v4` | officiel pruned int8 | Turbo LoRA v4 (larryvrh) | 6-8 |
| `pdd` | officiel pruned int8 | PDD sidecar fbjr (corrigé 29/08) | 4-8 |
| `hybrid` | hybride DaSiWa (v1/8Turbo/4Turbo) | déjà turbo (8Turbo/4Turbo) | 4-8 |

**Incompatibilités croisées** (jamais cumulables) :
- Turbo LoRA v4 ⊥ hybride (déjà turbo) ⊥ PDD — distillations non empilables.
- Turbo v4 / PDD : validés contre le **pruned int8 convrot** uniquement.
- Un hybride demandé force `hybrid` et **désactive** Turbo/PDD (garde
  silencieuse dans `config.env` ; le wizard ne propose jamais l'empilement).

## 3. Checkpoints hybrides (source primaire HF gated, repli CivitAI)

Fiche CivitAI `modelId=2877206` (« DaSiWa MiniMax H3 », Darksidewalker).
Repo HF personnel **`Kinderheim/private`** (HF_TOKEN) ; repli CivitAI
(CIVITAI_API_KEY) ; sans token → message manuel guidé, jamais bloquant.

| Variante | Fichier (nom publié) | SHA256 (CivitAI) | Taille | HF ? |
|---|---|---|---|---|
| `hybrid_v1` | `DasiwaMinimaxH3_dasiwaREF2VAHybridV1.safetensors` | `2255091d…74f25` | ~19,5 GiB | oui |
| `hybrid_8turbo` *(défaut director)* | `DasiwaMinimaxH3_dasiwaHybrid8turboV1.safetensors` | `e0441d26…aa6332` | ~19,5 GiB | oui |
| `hybrid_4turbo` | `DasiwaMinimaxH3_dasiwaHybrid4turboV1.safetensors` | `56c52c78…ed74f` | ~19,5 GiB | **non** (CivitAI only) |

CivitAI fileId : v1 = `3251526/3156811` · 8Turbo = `3275408/3159579` ·
4Turbo = `3272675/3156813`.

## 4. Mapping symlink (workflow jamais édité)

- **classic** : paires officielles → `diffusion_models/MiniMaxH3/<nom>` ;
  hybride v1 → `minimax_h3_{fl2va,ref2va}_pruned_int8_convrot.safetensors`.
- **director** : VAE int8 Kijai → `vae/MiniMaxH3/…` ; hybride →
  `dasiwa_minimax_h3_ref2va_v1_pruned_hybrid_bf16_m_8turbo_int8_row-wise_convrot_runtime_mixed.safetensors`
  (nom attendu par C-MMH3-18).

## 5. Option B — dépendances obligatoires

| Élément | Source | Taille |
|---|---|---|
| VAE vidéo int8 Kijai | `Kijai/MiniMax-H3-experimental` | ~2,95 GiB |
| Pack LBH (nœud) | `github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler` | clone |
| Upscaler latent 3D | `LBH-123-AI/Minimax_h3_latent_Upscaler` | ~659 MiB |

## 6. Persistance & garde

- Choix persistés dans `/workspace/.minimax_user_choices.env`
  (`H3_DASIWA_MODE`, `_SPEED_PATH`, `_CHECKPOINT_VARIANTS`,
  `_DIRECTOR_HYBRID_VARIANT`) — jamais les secrets (HF_TOKEN/CIVITAI_API_KEY).
- `_dasiwa_variant_guard` compare la **signature de sélection**
  `mode|speed|variants|director_variant` au marqueur disque ; refuse un
  changement silencieux sans confirmation ou
  `H3_DASIWA_ALLOW_VARIANT_CHANGE=true`.
- `_dasiwa_speed_guard` journalise le chemin vitesse résolu.
