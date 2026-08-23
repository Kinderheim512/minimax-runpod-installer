# Récapitulatif des changements de cette session

## Objectif
Ajouter, au preset `dasiwa_mmh3v12`, le choix entre les deux checkpoints
"pruned" officiels (Comfy-Org, comportement historique, inchangé par défaut
— un fichier FL2VA + un fichier REF2VA) et le checkpoint communautaire
unique **"DaSiWa Hybrid"** (darksidewalker, CivitAI), sans jamais toucher au
workflow ComfyUI lui-même.

⚠️ **Point important, corrigé en cours de session** : le checkpoint CivitAI
fourni n'est PAS un fichier réservé au rôle REF2VA — il fait aussi bien pour
le rôle FL2VA que pour le rôle REF2VA. La variante "pruned" télécharge donc
**2 fichiers** (un par rôle), tandis que la variante "dasiwa_hybrid" ne
télécharge **qu'1 seul fichier**, symlinké ensuite vers les deux noms
attendus par le workflow.

URL CivitAI fournie et utilisée comme valeur par défaut de l'option :
`https://civitai.red/api/download/models/3251526?fileId=3135537`.

## Fichiers modifiés

- `config.env`
  - Nouvelle variable `H3_DASIWA_CHECKPOINT_VARIANT` (défaut `"pruned"`,
    valeur alternative `"dasiwa_hybrid"`) + `H3_DASIWA_HYBRID_CIVITAI_URL`
    (l'URL ci-dessus, surchargeable) + `H3_DASIWA_HYBRID_FILENAME`.
  - `PRESET_DASIWA_MMH3V12` : les DEUX fichiers officiels (FL2VA et REF2VA,
    HuggingFace) ne sont ajoutés au manifeste QUE si la variante reste sur
    `"pruned"` — pour `"dasiwa_hybrid"`, aucun des deux n'est téléchargé,
    remplacés ensemble par l'unique fichier CivitAI (évite ~40 Go inutiles).
  - Nouveau tableau `PRESET_DASIWA_MMH3V12_CIVITAI_MODELS` (vide par défaut,
    peuplé d'une seule entrée si `"dasiwa_hybrid"` est actif) — format
    `"url|chemin_relatif_a_models/"`.
  - `PRESET_DASIWA_MMH3V12_SYMLINKS` : en mode `"pruned"`, deux liens vers
    deux fichiers réels distincts (comme avant) ; en mode `"dasiwa_hybrid"`,
    les DEUX liens (FL2VA et REF2VA) pointent vers le MÊME fichier réel
    unique — le nom vu par le workflow ne change JAMAIS dans les deux cas.

- `lib/presets.sh`
  - Nouvelle fonction `_preset_civitai_models_ref()` (même convention que
    `_preset_manifest_ref()`, `_preset_node_repos_ref()`, etc.).
  - `download_preset_models()` : le compteur global `[i/N]` inclut
    maintenant aussi les fichiers CivitAI additionnels ; une nouvelle boucle,
    après le téléchargement du manifeste HuggingFace, télécharge ces
    fichiers via `download_civitai_model()` (déjà existante dans
    `lib/models.sh`, utilisée depuis longtemps pour `MODEL_SOURCE=civitai`
    côté palier standard — aucune nouvelle logique de téléchargement créée).

- `wizard.sh`
  - Nouvelle question "DaSiWa preset — diffusion checkpoint(s) :" affichée
    uniquement quand le preset `dasiwa_mmh3v12` est sélectionné, avec deux
    choix : "Normal pruned — 2 official ... checkpoints, FL2VA + REF2VA"
    (défaut) et "Pruned, modified by DaSiWa — 1 community checkpoint
    covering both FL2VA and REF2VA" (expérimental). Le choix apparaît dans
    le résumé avant confirmation et est transmis à `install.sh` via la
    variable d'environnement `H3_DASIWA_CHECKPOINT_VARIANT` (même mécanisme
    que `SAGE_ATTENTION`/`INSTALL_SPECTRUM`, déjà utilisé par ce script).

- `CHANGELOG.md` — entrée sous `[Unreleased]` (anglais, suit la convention
  du fichier) décrivant cette fonctionnalité, avec le comptage correct des
  fichiers (2 vs 1).

- `README.md` — tableau des presets, transcript du wizard, et section
  "checkpoint variant en détail" mis à jour avec le comptage correct des
  fichiers et le nom de variable renommé.

## Correction apportée en cours de session
La première version de cette fonctionnalité supposait à tort que le
checkpoint CivitAI ne remplaçait que le REF2VA (d'où le nom initial
`H3_DASIWA_REF2VA_VARIANT`). L'utilisateur a précisé que ce même checkpoint
sert aussi bien pour FL2VA que pour REF2VA. En conséquence :
- la variable a été renommée `H3_DASIWA_CHECKPOINT_VARIANT` (plus
  seulement "REF2VA") ;
- le manifeste HuggingFace exclut maintenant les DEUX fichiers officiels
  (pas seulement REF2VA) quand `"dasiwa_hybrid"` est actif ;
- les DEUX liens symboliques attendus par le workflow pointent vers le même
  fichier réel unique en mode `"dasiwa_hybrid"`.
Testé en local (téléchargements simulés + création réelle des liens
symboliques) pour les deux variantes avant livraison.

## Non modifié (volontairement)
- Aucun fichier `.json` de workflow dans `presets/dasiwa_mmh3v12/` : le
  mécanisme de lien symbolique rend ce changement invisible pour le
  workflow lui-même, quelle que soit la variante choisie.
- `lib/lang/en.sh` / `lib/lang/fr.sh` : aucune nouvelle clé de traduction
  nécessaire — les messages CivitAI déjà existants
  (`models_civitai_downloaded`, `models_civitai_attempt_failed`,
  `models_civitai_final_failed`, `presets_downloading_models_step`,
  `presets_models_downloaded`) couvrent déjà ce cas dans les deux langues.

## Comportement par défaut
Sans rien changer à sa config, un utilisateur existant garde exactement le
même comportement qu'avant cette session (`H3_DASIWA_CHECKPOINT_VARIANT`
vaut `"pruned"` par défaut → les deux fichiers officiels Comfy-Org
téléchargés comme avant).

## Correctif additionnel : erreur 401 lors du téléchargement du checkpoint CivitAI
Retour utilisateur après un premier essai réel : le téléchargement du
checkpoint `dasiwa_hybrid` échouait systématiquement avec `curl: (22) The
requested URL returned error: 401`, jusqu'à épuisement des 5 tentatives.

**Cause** : `download_civitai_model()` (`lib/models.sh`) n'envoyait jamais
d'en-tête d'authentification. Ce checkpoint CivitAI précis est un contenu à
accès restreint (soumis à connexion, comme tout contenu marqué NSFW sur ce
site) — il nécessite donc une clé API CivitAI, faute de quoi le site répond
401 même si l'URL de téléchargement est par ailleurs correcte.

**Correctif** :
- `download_civitai_model()` envoie maintenant l'en-tête `Authorization:
  Bearer $CIVITAI_API_KEY` dès que cette variable d'environnement est
  définie — même convention déjà utilisée par `install_lora.sh` et
  `_download_preset_workflow_from_civitai()` (`lib/presets.sh`), qui ne
  s'appliquait simplement pas encore à cette fonction précise.
- La fonction distingue maintenant une erreur d'authentification (401/403 —
  nouveau message dédié invitant à définir `CIVITAI_API_KEY`) d'une erreur
  réseau/serveur classique (message générique inchangé).
- Nouvelles clés de traduction `models_civitai_attempt_failed_auth` /
  `models_civitai_final_failed_auth` dans `lib/lang/en.sh` et
  `lib/lang/fr.sh`.
- `config.env` (commentaire de `H3_DASIWA_HYBRID_CIVITAI_URL`), `wizard.sh`
  (avertissement affiché dès que `dasiwa_hybrid` est choisi) et `README.md`
  documentent maintenant explicitement ce besoin de `CIVITAI_API_KEY` pour
  cette variante précise.

Testé avec un serveur HTTP local simulant le comportement CivitAI (401 sans
en-tête `Authorization`, 200 avec la bonne clé) : le message d'erreur dédié
s'affiche bien sans clé, et le téléchargement réussit bien avec.
