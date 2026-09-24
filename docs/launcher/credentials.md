# Secure RunPod credential storage

The launcher keeps its secrets in a secure, per-user credential store: the
**RunPod API key**, the **private RunPod template ID**, and optional **extra
API keys** (`HF_TOKEN`, `CIVITAI_API_KEY` — extendable for future
model/workflow needs). Credentials are encrypted at rest with **Windows DPAPI**
and are no longer required in environment variables or `.env` files, and no
longer need to live in the RunPod template/pod secrets.

## What is stored, where

- **Stored** (one encrypted bundle): `runpod_api_key`, `runpod_template_id`,
  an optional `extra_secrets` map (e.g. `HF_TOKEN`, `CIVITAI_API_KEY`), plus a
  payload `version` (currently `2`; version `1` bundles remain readable and
  load with an empty map).
- **Never stored (secret store)**: `RUNPOD_POD_ID` and the GPU/pod-provisioning
  settings. Pod identity is transient and is held in the separate, non-secret
  pod registry (`<home>\pod.json`, see `docs/pod-provisioning.md`) — never in
  the DPAPI bundle.
- **Location**: `%APPDATA%\MiniMaxH3Launcher\credentials.dpapi` — deterministic,
  per-user, **outside the project folder** (never part of the repository, so
  regular commit/push of the project can never leak a key). Reinstalling or
  re-cloning the launcher does not delete the store; a different Windows user
  has no access.

## How it works

- `launcher/credentials.py` calls `CryptProtectData` / `CryptUnprotectData`
  through `ctypes` — no external dependency, no custom cryptography, no key
  material stored next to the data. The DPAPI key is bound to the Windows
  user account and the machine, so the ciphertext is unreadable anywhere
  else.
- The module imports cleanly on any platform. `CredentialStoreUnsupported`
  is raised only when the DPAPI store is actually used off-Windows; there is
  deliberately no insecure fallback.
- Writes are atomic (temporary file + `os.replace`) and **verified by reading
  the file back and decrypting it** before success is reported. DPAPI can in
  rare cases return success with corrupted output, so the app verifies.
- A best-effort user-only ACL (`icacls /inheritance:r /grant:r <user>:F`) is
  applied to the store directory and file on write — defense in depth on top
  of DPAPI's user binding.
- Load failures are classified with fixed, safe messages:
  `CredentialStoreMissing`, `CredentialStoreCorrupt`,
  `CredentialStoreMalformed`, `CredentialStoreVersionError` — none of them
  ever expose ciphertext or plaintext.

## CLI

```powershell
minimax-launcher credentials set      # prompts (hidden input): key + template ID (required),
                                   # then HF token + Civitai key (optional, empty keeps existing)
minimax-launcher credentials status   # prints CONFIGURED / NOT CONFIGURED / CORRUPT + SET/ABSENT
                                   # per extra key — never values
minimax-launcher credentials clear    # removes the store file (zero-overwrite + delete); idempotent
minimax-launcher credentials          # bare form = status
```

The GUI's "Identifiants" dialog offers the same four fields (masked entries;
empty keeps the stored value). After a one-time `credentials set`,
`minimax-launcher start` (and `status` / `doctor`) work with no RunPod variables
set at all.

## Precedence

Per secret: **environment variable → stored value → pod-side value → absent**.

- `RUNPOD_API_KEY` env, then stored `runpod_api_key`.
- `RUNPOD_TEMPLATE_ID` env, then stored `runpod_template_id`.
- `HF_TOKEN` env, then stored `extra["HF_TOKEN"]`; if the launcher has no
  value, a `HF_TOKEN` kept in the RunPod template/pod env is used as-is.
- `CIVITAI_API_KEY` env, then stored `extra["CIVITAI_API_KEY"]`.
- `RUNPOD_POD_ID` env or the pod registry (see `docs/pod-provisioning.md`) —
  never read from the credential store.
- When every managed secret is present in the environment the store is not
  even consulted.

## Where the keys are applied at start

- **Pod**: launcher-managed keys are injected into the pod env at creation,
  and a running/adopted pod whose stored value is absent or different is
  re-synced at start via the existing stop → env update (read-then-full-
  replace) → start sequence. The launcher only **adds/overrides** managed
  keys — it never deletes pod-side keys (removing a key from the RunPod
  template is the user's own operation). Identical values cause no pod
  mutation at all.
- **The pod env**: every `start` overlays the resolved keys onto the pod
  environment at creation, and re-syncs them (stop → update env → start) when
  one drifts. `HF_TOKEN` is what the pod-side installer authenticates with;
  `CIVITAI_API_KEY` is what it uses to download a Civitai model or LoRA. If a
  managed key is already correct the pod is not touched at all.
  reused, new values take effect on the next stop/start.

Existing setups that keep secrets in the environment or in the RunPod
template keep working unchanged; the launcher store is a superset, not a
migration break.

## Diagnostics

- `minimax-launcher status` prints a value-free `RunPod creds
  CONFIGURED|NOT CONFIGURED|CORRUPT` line.
- `minimax-launcher doctor` adds a `runpod_credentials` check
  (`OK` / `WARN` / `ERROR` / `SKIP`) with actionable guidance, e.g.
  "RunPod credentials are not stored (run 'minimax-launcher credentials set')".
- `load_config` degrades silently when the store is missing or the platform
  lacks DPAPI, and logs a single safe warning (no secret content) when a
  stored bundle is unreadable — environment values still apply.

## Security invariants

- The plaintext bundle never touches disk, logs, exceptions, status output,
  status output, or audit records.
- No secrets in `.env.example`, git, or any committed file; tests use
  generated fake credentials and an injected fake cipher (plus a real-DPAPI
  round trip with fake values on Windows).
- `Secrets` and `RunPodCredentials` reprs are redacted.

## Non-goals (explicitly out of scope)

- No pod identity, GPU selection, or data-center state stored here — pod
  bookkeeping lives in the non-secret pod registry
  (`docs/pod-provisioning.md`), which holds only the pod ID, name, timestamps,
  and GPU request.
- Credentials are never exposed to any other component: only the pod env
  and the RunPod client ever see a value.
