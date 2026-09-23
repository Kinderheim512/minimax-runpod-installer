# OpenFox Forge GUI

Graphical Windows front-end for the OpenFox Forge launcher: one window to see
the whole stack (pod, SSH tunnel, vLLM, model, OpenFox, SearXNG), start/stop it,
switch LLM profiles, manage credentials, and run diagnostics or repairs —
without touching the command line.

The GUI is a thin front-end: it reuses the exact same primitives as the CLI
and the MCP tool layer (`orchestrator`, `infra_diagnose`, `doctor`,
`infra_recover`, DPAPI `CredentialStore`). Stdlib-only at runtime, plus one
optional styling dependency:

```powershell
python -m pip install ".[gui]"     # ttkbootstrap: themed controls (optional)
```

With `ttkbootstrap` installed the window uses the ready-made `forge` theme
family and the *Paramètres rapides* « Apparence » switch flips it between
its dark and light variants live (no restart). Without it the GUI falls back
to the built-in palettes — same layout, same behaviour.

## Launching

### Development (no compilation)

```powershell
python -m launcher gui
# or, with the console script:
openfox-forge gui
```

Or double-click `OpenFoxForge-dev.vbs` (no console window; uses the project
`.venv` when present, else `pythonw` on PATH).

### Standalone executable

```powershell
python -m pip install ".[gui-build]"   # PyInstaller (build-time only)
python scripts/build_exe.py            # -> dist/OpenFoxForge.exe
```

Copy `dist\OpenFoxForge.exe` next to your `.env` (or anywhere: see
"Configuration" below) and double-click it. To also bundle the tray icon,
install the tray extra before building:

```powershell
python -m pip install ".[gui-build,tray]"
```

## Features

- **Dashboard (auto-refresh, ~5 s, toggleable)** — per-component state
  (`Pod`, `Tunnel SSH`, `vLLM`, `Modèle`, `OpenFox`, `SearXNG`,
  `Configuration`) plus the overall grade (`HEALTHY` / `DEGRADED` / `FAILED` /
  `STOPPED`) with cause and remediation, the OpenFox URL, and the credential
  store state (values are never shown). The primary start action is distinct
  from diagnostic and destructive actions, so terminating a pod is never
  visually confused with a normal stop.
- **Facturation (dashboard row)** — under the health table: how long the pod
  has been running, the estimated spend so far (elapsed time × hourly rate),
  the hourly rate, and the RunPod wallet balance with an estimate of how long
  the credits last. The balance comes from the RunPod GraphQL `myself`
  query (the `clientBalance` field — the REST v2 API does not expose it);
  the spend is an approximation of RunPod's per-second billing. The row
  turns orange under ~6 h of credits left, red under ~2 h, and keeps its
  last values while a transient probe fails (hidden only when nothing was
  ever fetched).
- **Retrouver une pile ouverte** — the ↻ refresh button (and the startup
  refresh) re-adopts a stack opened from another session (an earlier launcher
  instance, the CLI, or another machine sharing the account): when this
  session has no registered pod for the active workspace, every RUNNING
  launcher-managed pod on the RunPod account (identified by its
  `openfox-forge-<stack>-` name or its vLLM/ComfyUI environment) is listed;
  the one matching the active workspace is re-adopted automatically
  (bookkeeping only — no pod action) and the dashboard shows it again; an
  open stack of a *different* workspace is offered in the empty state with
  an « Adopter » button (confirmation → registry record + workspace switch).
- **Piles actives** — one row per workload stack (`Agent texte`,
  `Vidéo ComfyUI`, `GGUF llama.cpp`, `Entraînement LoRA`), each with its pod
  status, tunnel, readiness and hourly rate, its **own** Démarrer / Arrêter /
  Terminer buttons, and the cumulative cost/h underneath. Several stacks run
  at once — each on its own pod, tunnel and lifecycle — so a text-agent
  session can be open while ComfyUI generates or the training desktop is up.
  Starting one stack never touches another, and a running stack's row disables
  its own Démarrer (no double provisioning). The footer's mode radios only
  pick which tab is detailed.
- **Démarrer** — full startup sequence (provisions the pod when needed → SSH
  tunnel → vLLM → OpenFox → browser). Long operations run on a worker thread;
  the window stays responsive. It starts the stack the mode radios select —
  use the Piles actives rows to start another one.
- **Agent CLI (OpenFox / Codex)** — the footer's *Agent* selector records
  which local agent CLI the launcher treats as the active one (OpenFox by
  default), and each CLI has its own status toggle next to it:
  **grey = closed, green = open**; one click opens it, another closes it.
  Neither needs a pod — when the model endpoint answers the CLI is pointed at
  the tunnel, otherwise OpenFox starts against its own configured provider
  (e.g. a cloud API). Both toggles stay enabled in **every** mode, so the
  text agent can be opened while ComfyUI or the training desktop runs.
  Codex is a terminal UI, so opening it means opening a
  dedicated console window; it is configured purely through `-c` overrides
  (`model_provider`, `base_url`, `wire_api=responses`, `model`), so
  `~/.codex` is never written to. Both CLIs are refreshed at launcher startup
  with `npm i -g …@latest` — best-effort, on a background thread, never
  fatal, and switchable off in Paramètres.
- **Indicateur DeepSeek (en-tête)** — a whale with a round dot: **red during
  DeepSeek's peak hours** (tokens billed at full price), **green off-peak**
  (half price). Peak hours are Monday–Friday 01:00–04:00 and 06:00–10:00 UTC,
  straight from the official pricing page. The window is evaluated on
  **network time** — SNTP (UDP/123) first, then an HTTPS `Date` header, and
  only as a last resort the local clock — so a machine with a wrong clock
  still shows the right state; the tooltip always names the source in use
  along with the current UTC time and the next switch (UTC and local).
- **Arrêter** — stops OpenFox, the SSH tunnel, and the registered pod (disk
  kept). **Terminer le pod** (right next to it, and never next to *Quitter*)
  — additionally terminates the pod (with confirmation) to free the GPU.
- **Profil LLM** — `fp8` (default) / `int4-autoround` / `uncensored`. Selecting
  `uncensored` shows its operator note (gated repo, HF_TOKEN). Starting with a
  different model than the one the pod currently serves asks for confirmation
  (the launcher performs stop → update env → start, keeping the pod's disk
  cache).
- **Docteur** — non-destructive diagnostics, printed to the in-window journal.
- **Réparation** — `Démarrer le pod`, `Reconnecter SSH`, `Redémarrer OpenFox`,
  `Relancer ComfyUI`. The row is only shown when `FORGE_INFRA_RECOVERY=confirm`;
  every action asks for confirmation and reports before/after.
- **▶ Réparer (en un clic)** — while the same condition holds
  (`FORGE_INFRA_RECOVERY=confirm`), a cause the launcher can repair gets a
  shortcut button right next to the « Cause : … → Remédiation : … » hint: the
  failing component is mapped onto the matching recovery action (stopped pod →
  *Démarrer le pod*, dead tunnel or failing vLLM → *Reconnecter SSH*, stopped
  OpenFox → *Redémarrer OpenFox*, failing ComfyUI → *Relancer ComfyUI*). The
  button runs the same audited action as the row above, confirmation included,
  and stays hidden when nothing maps (SearXNG, configuration) or while another
  action is running.
- **Identifiants RunPod** — shows the store state without values;
  **Configurer** (masked fields, DPAPI-encrypted, empty field keeps the stored
  value) and **Effacer** (with confirmation).
- **.env** — shows which `.env` file is in use (or `aucun (facultatif)`).
  **Créer .env** writes a starter file (copied from `.env.example` when
  present) next to the executable/repo, then reloads it; **Éditer .env** opens
  it in the default editor.
- **Minuteur** — enter a duration in minutes and start; when it expires the
  launcher stops every registered pod (plus the local tunnel/OpenFox), with
  retries, and only reports success once no pod is billing anymore. The
  deadline is absolute (it survives Windows sleep) and is persisted, so a
  stop missed while the launcher was closed fires automatically on the next
  start; a failed stop keeps the schedule for retry and is flagged loudly.
  An optional « Après l'arrêt du pod » action can then **Éteindre Windows**
  or **Mettre Windows en veille**, executed only after the pod stop confirms.
- **Journal** — live launcher log in the window. Every line passes through the
  standard secret redaction, so no API key/token ever appears. The OpenFox
  process's own output is captured and relayed here with an `[openfox]`
  prefix (see below: OpenFox runs without any console window).
- **Bibliothèque (LoRA / Workflow / Node)** — a card per asset, and each card
  carries a **checkbox**: checked = the asset is pushed to the pod
  automatically at creation (`H3_CUSTOM_LORAS` / `_NODES` / `_WORKFLOWS`),
  unchecked = it stays in the library but is never sent to a new pod — it can
  still be installed on demand with the ⬇ button. The toolbar has
  « ☑ Tout cocher » / « ☐ Tout décocher » and a live
  « N actif(s) / M — téléchargés au lancement du pod » counter. The state is
  saved in `gui.json`, travels in the JSON backup, and disabled entries are
  marked in the Markdown export. Editing an entry never silently re-enables
  it.
- **Bibliothèque — source URL *ou* fichier local** — le formulaire a un choix
  de source explicite : *URL* (le pod télécharge lui-même au lancement, via
  `H3_CUSTOM_*`) ou *Fichier local* (bouton **📂 Parcourir…** : un fichier
  `.safetensors` pour un LoRA, un `.json` pour un workflow, un **dossier**
  pour un node). Une entrée locale est **envoyée par le launcher** sur le pod
  — le pod ne peut pas lire votre disque, et son installateur refuse tout ce
  qui n'est pas http(s) :
  - destinations identiques à la voie URL : `models/loras/personal/`,
    `user/default/workflows/personal/`, `custom_nodes/<nom>/` ;
  - un node en dossier est envoyé récursivement, puis son
    `requirements.txt` est installé dans le venv du pod (comme le fait le
    clone git) ;
  - l'envoi passe par **le tunnel SSH déjà ouvert** (aucun port en plus) ;
  - un fichier déjà présent **avec la même taille** est sauté, donc un
    « Démarrer » ne renvoie pas 2 Go ;
  - l'envoi automatique a lieu **après** l'ouverture de ComfyUI dans le
    navigateur, il est **non bloquant** (un échec n'empêche pas le démarrage
    de réussir) et le journal donne nom, taille et issue ;
  - le badge de la carte indique `URL` ou `Local`.
- **Bibliothèque — « 📋 Coller »** — ajoute les entrées présentes dans le
  presse-papiers. C'est le pendant du userscript
  `userscripts/openfox-forge-annuaire.user.js` (Tampermonkey) : sur une page
  Civitai / civitai.red / Hugging Face, son bouton flottant lit les
  métadonnées du modèle, laisse choisir **la version puis le fichier** à
  télécharger (un modèle Civitai peut exposer plusieurs versions — normale,
  pruned, bf16/fp16 — sur la même page), puis copie une entrée complète :
  nom, lien de page, lien de téléchargement, trigger words, modèle, mode et
  note. Le collage passe par **la même fusion que « 📥 Importer »** :
  déduplication par `type` + `nom`, et **aucune entrée existante n'est
  supprimée** (un presse-papiers invalide est refusé sans rien écrire).
- **Bibliothèque — rendu** — la grille **réutilise ses cartes** : filtrer,
  redimensionner la fenêtre ou changer d'onglet ne recrée plus les widgets.
  Mesuré sur 36 entrées (481 widgets) : ouverture de l'onglet ~0,4 s
  (contre ~0,95 s), 8 redimensionnements ~1,5 s (contre ~8 s), 5 frappes
  dans le filtre ~0,17 s (contre ~5 s). Les cartes sont construites pendant
  l'inactivité du démarrage, et le filtre s'applique après une courte pause
  de frappe plutôt qu'à chaque touche.
- **Entraînement LoRA (tab)** — the training stack's own surface: the resolved
  image / desktop URL / files URL / registered pod, « Ouvrir Fizgig ↗ » and
  « Fichiers ↗ » (both built from the *configured* ports), « Vérifier le
  template », the **pre-download checkboxes** (Krea 2 / Klein 9B / outils —
  ~45 GB at boot, so the cost is visible and off by default), the local LoRA
  collection folder, and the collect/list actions. The dashboard health table
  speaks the stack's own vocabulary (Desktop / Image), with no SearXNG or
  agent row.
- **Paramètres rapides** — the cog in the header (or `Ctrl` + `,`) groups the
  light/dark theme, automatic status refresh, the startup agent-CLI update
  (`npm i -g openfox/codex@latest`), and close-to-tray behavior in one place.
  These choices are remembered in `%APPDATA%\OpenFoxForge\gui.json`.
- **Navigation** — the header provides a direct refresh control, while the
  Application and Navigation menus give keyboard-friendly access to settings
  and each workspace tab.
- **Icône** — the window title bar *and* the taskbar use the native multi-size
  brand icon (`launcher/icon.ico`, 16–256 px; embedded in the exe), so the
  app never shows the default `pythonw` icon in the taskbar.
- **Pas de fenêtre console** — OpenFox is spawned with `CREATE_NO_WINDOW`
  (its npm shim is a `.CMD` that would otherwise open a stray `cmd.exe`
  window); the SSH tunnel already ran windowless. In CLI mode
  (`python -m launcher start`) OpenFox keeps inheriting your terminal
  instead, so no behavior change there.
- **Barre des tâches** — when `pystray` is installed (extra `[tray]`), the
  window minimizes to a tray icon on close (checkbox), with a menu:
  Afficher / Démarrer / Arrêter / Quitter. Without pystray, closing the window
  quits (the checkbox is automatically disabled and the journal says why).

## Configuration

The GUI reads the same environment variables as the CLI (see
`.env.example`). On top of that, at startup it loads a `.env` file if found,
in this order:

1. `OPENFOX_FORGE_ENV_FILE` (explicit path override);
2. the executable's directory (packaged build) — for development runs, the
   repository root;
3. the current working directory.

Variables already present in the environment always win (same precedence
principle as the secrets). RunPod secrets still come first from the
environment, then from the DPAPI credential store (`credentials set`).

## Build layout

| File | Purpose |
|---|---|
| `launcher/gui.py` | The GUI (tkinter) + all non-UI logic (env loading, settings, status snapshot, actions) |
| `launcher/main.py` | `gui` subcommand wiring (`openfox-forge gui`) |
| `launcher_gui_entry.py` | PyInstaller target script (calls `launcher.gui.main`) |
| `scripts/build_exe.py` | PyInstaller build → `dist/OpenFoxForge.exe` (onefile, no console) |
| `OpenFoxForge-dev.vbs` | Console-free dev launcher (double-click) |
| `scripts/_gui_smoke.py` | Headless smoke test: launch → window → screenshot → pixel check → WM_CLOSE → clean exit |
| `scripts/_gui_state_check.py` | Verifies the dashboard populates from live probes |
| `scripts/_exe_smoke.py` | Verifies the packaged exe: launch → window → WM_CLOSE → minimized to tray |

## Tests

`python -m pytest tests/test_gui.py` covers the non-UI logic (env-file
parsing/discovery, settings persistence, log redaction, status snapshot
mapping, action delegation, CLI dispatch) and runs a tkinter smoke (skipped
automatically when no display is available).
