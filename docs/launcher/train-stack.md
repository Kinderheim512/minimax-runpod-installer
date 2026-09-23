# LoRA training stack (stack `train`)

The launcher serves four independent workload stacks, selected with `FORGE_STACK`
(or `--stack` / the GUI mode selector):

| Stack | What runs on the pod | Local result |
|---|---|---|
| `agent` (default) | vLLM serving Qwen3.8-27B | OpenFox pointed at the tunneled model |
| `comfy` | ComfyUI + the MiniMax H3 toolchain | the ComfyUI web UI (tunnel or direct) |
| `llamacpp` | `llama-server` serving a GGUF quant | OpenFox pointed at the tunneled model |
| **`train`** | **Fizgig — a LoRA training desktop** | **the Fizgig UI, and trained LoRAs collected locally** |

## What it is

Trains **LoRA adapters** — of a person, a style, a voice — from ordinary photo,
video-clip and audio datasets, on a rented RunPod GPU, driven from the launcher.

The engine is **[Fizgig](https://github.com/shootthesound/Fizgig)** (Apache-2.0), which
trains MiniMax H3 (33B), Krea 2 and Flux 2 Klein 9B. Its output is a kohya
`.safetensors` that loads straight into ComfyUI — including the MiniMax H3 checkpoint
the `comfy` stack already runs, so a trained LoRA is immediately deployable.

**The launcher writes no trainer.** It rents the GPU, builds the tunnel, and collects the
result. Everything about training itself is Fizgig's job.

## The two ports, and why they are never published

The pod runs two services:

| Port | What | How it is reached |
|---|---|---|
| `6080` | KasmVNC desktop (the Fizgig UI) | the launcher's SSH tunnel, on `127.0.0.1:TRAIN_LOCAL_PORT` |
| `8080` | filebrowser, rooted at `/workspace` | the launcher's SSH tunnel, on `127.0.0.1:TRAIN_FILES_LOCAL_PORT` |

Both are forwarded by **one `ssh -L ... -L ...` process** (see
`launcher.tunnel.build_tunnel_args_multi`). Two ssh processes would mean two
authentications, two failure modes and two things to reap for a single pod.

**Neither port is ever published on the pod's public URL.** This is a deliberate
difference from Fizgig's stock template, which exposes both publicly (password-gated, but
public). There is no `direct` access mode for this stack, and there must not be one:
unlike ComfyUI, this endpoint is a full desktop running as root.

## The image, and why it is upstream's

The stack runs **upstream's own image**, `ghcr.io/shootthesound/fizgig`.

It used to run a fork: `ghcr.io/kinderheim512/openfox-forge-train`, which baked Fizgig at
an audited commit with its git remote removed and the entrypoint's update block replaced by
a restore-and-verify. That fork was **retired**, for one reason: upstream publishes every
couple of days, a fork can only ever lag the cadence it would have to keep up with, and
rebuilding it needed a CI run the account could not sustain. The fork spent its life frozen
a few releases behind — the opposite of what it was for.

What that costs, stated plainly: **the image is no longer audited by us.** The base image
is not pinned by digest, the binaries are not hash-pinned by us, and the application is not
pinned at all (see `FIZGIG_REF` below). What replaces the audit is not a claim — it is a
readable answer: the launcher reports **which Fizgig the pod is actually running**, so a
run that moved is visible rather than silent.

## Privacy

- **telemetry is off** — `HF_HUB_DISABLE_TELEMETRY=1` is set by the launcher's pod env,
  because upstream's image does not bake it. It covers `huggingface_hub` (via
  `transformers`), the only third-party telemetry in the dependency tree;
- the **only outbound request that carries data** in the whole application is Fizgig's own
  pod-stop mutation, and only when the user enables it;
- trained LoRAs are collected **locally** — never through a HuggingFace repo or any other
  third party;
- **both ports stay tunnel-only.** Upstream's stock RunPod template publishes 6080 and 8080
  on the pod's public URL; this launcher's template does not. That difference is enforced
  by the template checks, not by convention — see `scripts/train/template.spec.json`.

## Versioning: two independent axes

Conflating these is the easy mistake.

| Axis | What it is | Default | How it moves |
|---|---|---|---|
| `TRAIN_IMAGE_TAG` | the **runtime** — CUDA, PyTorch, KasmVNC, system packages | `latest` | upstream republishes the tag; a new pod pulls it |
| `TRAIN_FIZGIG_REF` | the **application** — Fizgig itself | `master` | upstream's entrypoint pulls it at **every pod boot**, so a restart is the update |

**Both float, on purpose, and they are meant to move together.** The old rule — never
`:latest` — came from the era when this was *our* image: the tag was the only version axis
and the audit hung off it, so pinning was a security property. Both premises are gone.

Pinning only the runtime was in fact incoherent: the application floats, so a pinned base
would sit there rotting while the app raced ahead of it, and someone would have to bump the
constant by hand — the maintenance burden the floating ref exists to remove, re-introduced
on the other axis.

What floating the runtime costs: a pod created today can boot a different base than one
created yesterday, and **a broken upstream image breaks the next pod creation** — which is
worse than a broken app, because it fails before the tunnel exists and no version check can
look inside. The rollback is to set `TRAIN_IMAGE_TAG` to a concrete tag; that is the whole
story. Pin either axis when a run must be reproducible.

Because the application floats, **updating Fizgig is a pod restart**, and the
**« Version Fizgig »** button in the GUI's *Entraînement LoRA* tab exists to make the drift
visible before it surprises anyone: it reads the pod's revision over SSH
(`git describe --tags`, so `6.0.1` or `6.0.1-3-gabc1234` when master has moved past the tag)
and compares it with upstream's newest release from the GitHub API. A pod behind the newest
release is reported with the action attached — restart it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FORGE_STACK` | `agent` | Workload stack (`agent` \| `comfy` \| `llamacpp` \| `train`) |
| `RUNPOD_TRAIN_TEMPLATE_ID` | (credential store) | private RunPod template for this stack |
| `TRAIN_LOCAL_PORT` | 6080 | local port forwarded to the desktop |
| `TRAIN_FILES_LOCAL_PORT` | 8081 | local port forwarded to the file manager — **not 8080**, which the llama.cpp stack already forwards locally (a second `ssh -L 8080:…` aborts the whole train forward) |
| `TRAIN_FETCH_MODELS` | *(empty)* | families the pod pre-downloads (`krea2`, `klein`, `tools`) |
| `TRAIN_IMAGE_TAG` | `latest` | runtime tag the template must reference — floating, like `FIZGIG_REF` |
| `TRAIN_FIZGIG_REF` | `master` | which Fizgig the pod runs — pulled at every boot, so a restart updates it |
| `TRAIN_VOLUME_GB` | 150 | volume size the template must provide |
| `TRAIN_LORA_DIR` | *(empty)* | local folder trained LoRAs are collected into |
| `TRAIN_ALLOW_IN_APP_AUTOSTOP` | `false` | let Fizgig stop its own pod (see below) |
| `TRAIN_REQUIRE_SSH` | `true` | fail with a clear message when the template has no SSH |
| `VNC_PASSWORD` | (credential store) | desktop + file-manager credential, 12+ characters |

`RUNPOD_POD_ENV` (JSON) always overrides the above for the pod env.

### Cost: one stop mechanism, not two

Fizgig can stop its own pod when a training run finishes, and so can the launcher. Two
independent stop mechanisms racing over one machine is a support burden with no upside, so
**the launcher owns it by default** (it also knows the tunnel state) and the in-app one
stays off. Set `TRAIN_ALLOW_IN_APP_AUTOSTOP=true` to flip that — but flip it, do not run
both.

## Prerequisites

1. **A private RunPod template**, created by hand from
   `scripts/train/template.spec.json`. Key fields: image
   `ghcr.io/shootthesound/fizgig:latest`, ports
   `22/tcp,6080/http,8080/http`, volume ≥ 100 GB at `/workspace`, **SSH access enabled**,
   and `FIZGIG_REF` set explicitly.
2. **The template ID stored** with `openfox-forge credentials set` (field *RunPod Train
   template ID*) or exported as `RUNPOD_TRAIN_TEMPLATE_ID`.
3. **SSH enabled on that template.** The image deliberately leaves port 22 closed unless
   `PUBLIC_KEY` is set (upstream's security default). Without it nothing listens, the
   tunnel can never come up, and the stack is unreachable. RunPod's *SSH Terminal Access*
   toggle sets `PUBLIC_KEY` for you.

### The template, field by field

The reference template (`aolnvq5akk`, "Training lora") is configured exactly like this.
The API returns the field names on the left; the console shows the same settings under
their usual labels.

| Setting | Value | Why |
|---|---|---|
| Image | `ghcr.io/shootthesound/fizgig:latest` | upstream's image, floating on purpose (see *Versioning* above). Never a **digest** — RunPod rejects pod creation from a digest-pinned image |
| Container disk (`disk` / *Container Disk*) | **40 GB** | the weights live on the volume |
| Volume (`mounts.persistent` / *Volume Disk*) | **150 GB at `/workspace`** | MiniMax H3 weights are ~45 GB before any dataset, latent cache or checkpoint. Below 100 GB the stack cannot hold a full run |
| Ports | `22/tcp`, `6080/http`, `8080/http` | 22 is the tunnel (without it the stack is unreachable), 6080 the KasmVNC desktop, 8080 filebrowser — both reached **only** through that tunnel, unlike upstream's stock template which publishes them |
| SSH (*SSH Terminal Access*) | **enabled** (`startSsh: true`) | RunPod then injects `PUBLIC_KEY`, which is the only thing that makes the image start sshd |
| `HF_HUB_DISABLE_TELEMETRY` | `1` | upstream's image does not bake it, so the template is the only place this guarantee can live |
| `FIZGIG_REF` | `master` | **the update mechanism.** Upstream pulls this ref at every boot, so a restart is the update. Set explicitly so the launcher can report which ref the pod was meant to use |
| `HF_TOKEN` | `{{ RUNPOD_SECRET_huggingface }}` | a **reference** to the account secret, never the value. Needed for the licence-gated Klein repos |

**Deliberately NOT in the template:**

* `VNC_PASSWORD` — a secret. The launcher injects it from the DPAPI store at pod
  creation (`Paramètres → Identifiants → Mot de passe bureau d'entraînement`, 12+
  characters). Unset, the entrypoint generates one and prints it in the pod log.
* `FETCH_MODELS` — driven by the tab's pre-download checkboxes, so the cost decision is
  made per launch rather than frozen in the template.
* `PUBLIC_KEY` — RunPod sets it when SSH is enabled; setting it by hand is how you lock
  yourself out.
* `FIZGIG_REPO` — upstream's git URL override. It would run Fizgig from somewhere the
  launcher never chose; the template checks reject it.

**GPU (VRAM) is NOT part of the template** — it is chosen at launch (the launcher's GPU
selector, `TRAIN_GPU`/the GUI field). Fizgig trains a MiniMax H3 LoRA on **16 GB** with
block swap, but block swap moves weights over PCIe every step and costs roughly 4× the
step time: a 32 GB card (RTX 5090) finishes the same LoRA far cheaper per run. H100/A100
are poor value here — LoRA training never touches 80 GB.

Verify the template before starting:

```bash
python scripts/train/verify_template.py --dry-run   # what it should contain
python scripts/train/verify_template.py             # check the stored one
```

The same checks run from the GUI's **Vérifier le template** button — one implementation
(`launcher/train_template.py`), two entry points, so the button and the terminal can never
disagree.

## Usage

```bash
# CLI
openfox-forge start --stack train
openfox-forge status          # stack-aware
openfox-forge stop            # stops the registered train pod
openfox-forge doctor          # includes the train checks

# GUI
openfox-forge gui             # mode selector: … / « Entraînement LoRA »
```

In the GUI, the **Entraînement LoRA** tab shows the configured image and endpoints, opens
the desktop or the file manager, verifies the template, and collects trained LoRAs into a
local folder you choose.

### From an agent session

The stack is also reachable from an OpenFox session, so a text agent can bring the training
pod up while it works on something else:

* `forge_stack_start` with `stack="train"` launches the stack (detached — it returns at
  once, poll `forge_status`), using the ports, image and GPU persisted in `gui.json`;
* `forge_train_status` reports the disk, the **Fizgig version the pod actually runs**, the
  datasets and the trained LoRAs, and collects the LoRAs into the configured local folder;
* `forge_stack_stop` with `stack="train"` stops that pod **only** — a running text-agent
  session and its own tunnel are left alone.

**What it deliberately does not do: start a training run.** There is no headless training
entry point here — the launcher writes no trainer, and everything about training itself is
Fizgig's job. The agent brings the pod and the desktop up and reports on it; the run is
driven in the Fizgig UI at `http://127.0.0.1:TRAIN_LOCAL_PORT`.

## Getting a dataset in, and a LoRA out

### The weights come first

`FETCH_MODELS` is **empty by default**, so a fresh pod boots with `/workspace/models/`
**empty** — Fizgig then has nothing to train with, dataset or not. Two ways to fill it:

* on the pod, immediately, without a restart —
  `cd /workspace/Fizgig && PYTHONPATH=/workspace/Fizgig/src python3 -m fizgig.scripts.fetch_models --family <krea2|klein|minimax|tools> --progress`
  (`--all` for every family; `--include-optional` adds the Krea 2 Turbo and MiniMax ref2va
  DiTs. Klein is gated, so it needs `HF_TOKEN`);
* at pod boot, with `TRAIN_FETCH_MODELS` (`krea2,klein,tools`, …) — the launcher notices the
  drift and does stop → update env → start.

The GUI's **Entraînement LoRA** tab exposes all four families: `krea2`, `klein`, `minimax`
and `tools`. MiniMax H3 is ~45 GB and Klein is gated (`HF_TOKEN`), so only check what the
target LoRA actually uses — the on-pod path above is finer-grained and needs no restart.

### Reaching the pod from an agent

The two services are never published: they answer **only** through the launcher's SSH
tunnel, and they are authenticated.

| Interface | URL | Credentials |
|---|---|---|
| KasmVNC desktop (Fizgig) | `http://127.0.0.1:TRAIN_LOCAL_PORT` (6080) | user **`fizgig`** + `VNC_PASSWORD` |
| filebrowser | `http://127.0.0.1:TRAIN_FILES_LOCAL_PORT` (8081) | user **`admin`** + the **same** `VNC_PASSWORD` |

`VNC_PASSWORD` is the launcher's *desktop password* (DPAPI store,
`openfox-forge credentials status`); the entrypoint reuses it for both services. A 401 or
`ERR_INVALID_AUTH_CREDENTIALS` means the credentials were missing — not a broken pod.

For a file transfer, `scp` goes to the pod's **public SSH endpoint** (the tunnel forwards
6080 and 8080 only), which means: user **`root`**, the launcher's key
(`-i "$SSH_KEY_PATH"`, default `~/.ssh/id_ed25519`), and the pod's **public SSH port** —
never `22`. Resolve the endpoint from the launcher rather than hard-coding it, since RunPod
reassigns it on restart:

```bash
python -c "
from launcher.config import load_config
from launcher.credentials import CredentialStore
from launcher.runpod import RunPodClient
from launcher.pod_registry import PodRegistry
cfg = load_config(stack='train', store=CredentialStore())
pod = RunPodClient(cfg.secrets.runpod_api_key, timeout=20.0).get_pod(
    PodRegistry().load('train').pod_id)
e = pod.ssh_tunnel_endpoint()
print(f'{e.username}@{e.host} -p {e.port} -i {cfg.ssh.key_path}')"
```

**In:** `scp -r -P <port> -i "<key>" "<local>/. " root@<host>:/workspace/datasets/<name>/`
(the trailing `/.` copies the folder's *contents*), or drag a folder into the file manager.
Put one folder per LoRA under `/workspace/datasets/`, images and `.txt` captions together.
Clips and voice recordings go in the same folder — Fizgig trains photos, clips and audio in
one run. Verify with `TrainOps.list_datasets()` (or `forge_train_status` `action=datasets`).

**Out:** the **Récupérer les LoRA** button (or `TrainOps.collect_loras`) copies everything
in `/workspace/output_loras` into your chosen local folder with `scp`, skipping files
already collected so a re-run resumes rather than re-fetches.

Fizgig writes per-epoch checkpoints (`<name>-000012.safetensors`) beside the final file, so
"collect everything" is usually what you want: the final LoRA is not always the best epoch,
and Fizgig's LoRA Royale tool exists precisely to compare them.

## Storage

Models, datasets, checkpoints and the latent cache all live under `/workspace`, which must
be a **volume**, not the container disk — the container disk is erased when the pod stops.
MiniMax H3 weights are ~45 GB before any dataset or checkpoint, hence the 150 GB default.

**Stop** the pod between sessions; do not **terminate** it. A volume disk goes with its
pod. A network volume survives termination but is region-locked.

## Diagnostics

`doctor`, `forge_infra_diagnose` and the GUI health table all understand this stack. The
recovery action is `restart_train` (MCP tool `forge_infra_recover`, action
`restart_train`): it starts a stopped pod, resolves the SSH endpoint, and re-establishes
the **two-port** tunnel, verifying that both ports answer.

The tunnel-name-keyed hint matters here: when a train tunnel never comes alive, the error
names the SSH-access requirement rather than leaving you with a generic timeout — that is
by far the likeliest cause.

## Validation

Everything below the line is **static** and runs in CI. The rest needs a real pod.

**Static (covered by `tests/test_train_stack.py`, `tests/train/` and
`tests/test_fizgig_version.py`):**

- `TrainConfig` declaration, defaults and validation (ports differ, tag is not `:latest`,
  volume positive, `FIZGIG_REF` present in the pod env);
- two-port tunnel construction: one `-N`, one `-L` per target, in order, one process;
- every local port checked before ssh is spawned;
- `is_alive`/`wait_alive` with no target require **every** forwarded port;
- `TrainOps` listing, name validation (a name must come from the pod's own listing, never
  free text), scp arguments, partial-file cleanup, resume-on-recollect;
- the template checks, and that `template.spec.json` describes a template they accept —
  including that the **retired fork image is rejected** and that `FIZGIG_REF` is required
  while `FIZGIG_REPO` is refused;
- `launcher.fizgig_version`: version parsing, `git describe` shapes, release-feed parsing,
  and every comparison outcome — no network, the opener is injected;
- `restart_train` on the right and wrong stacks, both ports through one call, externally
  held port refused, dead tunnel reported as a verification failure.

**Needs a pod (the end-to-end validation):**

1. `openfox-forge start --stack train` reaches the desktop and opens it.
2. `git -C /workspace/Fizgig describe --tags --always` (or
   `TrainOps.pod_fizgig_version()`) reports a version, and the GUI's *Version Fizgig*
   button agrees with it.
3. Ports `6080`/`8080` are **unreachable** from the pod's public RunPod URL and reachable
   only through the tunnel.
4. End to end: ~20 photos → a MiniMax H3 LoRA trained on the pod → collected locally →
   installed on the `comfy` pod → a generated clip showing the person.

Record the outcome of 1–4 here when it has been run.
