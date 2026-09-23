# Automatic RunPod pod provisioning

The launcher can create its own RunPod pod instead of requiring
`RUNPOD_POD_ID`. This document describes the lifecycle, the ownership model,
the single-machine lock, and the crash-safe bookkeeping that make automatic
provisioning safe.

## How a pod is resolved (priority order)

Every launcher entry point (`start`, `stop`, `status`, `doctor`, and the MCP
tools) resolves the pod in the same fixed order:

1. **`RUNPOD_POD_ID`** (environment) — used exactly as given. If that pod does
   not exist, the launcher fails with an actionable error; it never falls
   through to provisioning, and it never stops or terminates an
   environment-configured pod.
2. **The pod registry** (`<home>\pod.json`) — the pod this launcher previously
   created on this machine. If the record points at a pod that no longer
   exists or is `TERMINATED`, the record is cleared and provisioning is
   attempted.
3. **Automatic provisioning** — only when a template ID is configured
   (`RUNPOD_TEMPLATE_ID`, env or credential store). Without one, the launcher
   reports which variable to set.

The registry lets a single `start` create the pod and every later invocation
(possibly a different CLI process) find and manage it without `RUNPOD_POD_ID`.

## Provisioning flow

When resolution reaches step 3, the launcher:

1. Chooses a pod name: `RUNPOD_POD_NAME` if set, otherwise
   `openfox-forge-<YYYYMMDD-HHMMSS>`.
2. Acquires the **single-machine provisioning lock** (see below) with intent
   `provision` and the chosen name.
3. Re-checks the registry **while holding the lock** — if a competing launcher
   on the same machine already created a live pod, that pod is reused and no
   second pod is created.
4. Calls the RunPod create-pod API with the configured template, GPU, GPU
   count, preferred data centers, and any per-pod environment overrides
   (`RUNPOD_POD_ENV`).
5. Records the pod in the registry **immediately after the create call
   succeeds** (before any readiness wait), so a crash at any later point can
   never leave an untracked pod behind.
6. Waits for the pod to become `RUNNING` (bounded by
   `RUNPOD_PROVISION_TIMEOUT`, default 1800 s).

The created pod is **never started** by the launcher — a freshly created pod
provisions on its own; the launcher only waits for it. (A pre-existing pod
found via the registry is started if its status is startable, exactly like the
pre-provisioning behaviour.)

## Ownership and what the launcher will and will not do

The launcher keeps a narrow, explicit ownership boundary:

| Action | Environment pod (`RUNPOD_POD_ID`) | Registered pod (created here) |
|---|---|---|
| `start` (start if needed) | yes | yes |
| `start` (create if missing) | no | yes |
| `stop` (default) | no | yes — stop, record kept |
| `stop --terminate` | no — refused | yes — terminate, record cleared |
| rollback on failed `start` | no | only a pod created *in this invocation* |

A pod created in this invocation is stopped once if a later startup stage
fails (e.g. no SSH endpoint, tunnel failure, vLLM not healthy). If the pod's
state cannot be verified (the API is unreachable), it is left untouched — the
original error is always preserved and a verification failure is never
allowed to mask it or to mutate a pod blindly.

## The provisioning lock

The lock (`<home>\provision.lock`) prevents two launcher processes on the same
machine from racing to create a pod. It is acquired **only** on the create
path — never when an environment pod or a live registered pod is reused. The
lock records the intent and pod name for diagnostics, is released in all
paths (including failures), and is deliberately **not** a cross-machine
distributed lock: it protects a single workstation, where a double-create
would otherwise be possible. (A stale lock file can be deleted by hand; it is
not a credential and holds no secrets.)

## Crash safety

The ordering "create → record → wait" guarantees that a crash leaves the pod
tracked, so `openfox-forge start` (or `status`/`doctor`) can recover it.
Concretely:

| Crash point | State left behind | Recovery |
|---|---|---|
| During create (API call fails) | no pod, no record | retry `start` |
| After create, before record write | pod exists, tracked in-memory only | rollback stops it (record write is also attempted first) |
| During readiness wait | pod + record | next `start` reuses the recorded pod |
| During tunnel/vLLM/OpenFox | pod + record | rollback stops a pod created this run; `stop` stops the recorded pod |

## Orphan recovery

If a created pod is ever truly orphaned (for example the registry file was
deleted but the pod is still running):

- Set `RUNPOD_POD_ID` to its ID to manage it explicitly (the launcher will
  not terminate an environment pod), or
- Stop or terminate it from the RunPod console / API, or
- Use the RunPod list-pods endpoint to find its ID by the
  `openfox-forge-…` name.

## Clearing the registry by hand

There is no CLI subcommand for clearing the pod registry; `registry.clear()`
is an internal call the launcher makes automatically (when a registered pod is
found to be gone or `TERMINATED`, or after `stop --terminate`). To force a
fresh provision manually, delete the registry file itself — its exact path is
`%APPDATA%\OpenFoxForge\pod.json` (or `$OPENFOX_FORGE_HOME\pod.json` when that
variable is set) — and then run `openfox-forge start`. Alternatively, set
`RUNPOD_POD_ID` to adopt an existing pod instead of provisioning. Both are the
actions named in the "registered pod is unreachable" error message.

## Cost note

- `stop` keeps the pod's disk and makes the GPU cost zero; the pod can be
  started again later from the registry.
- `terminate` deletes the pod and its disk (destructive). The launcher only
  terminates a pod it created on this machine, and only via the explicit
  `openfox-forge stop --terminate` command.

## API behaviour the launcher relies on

The create request sends the configured `dataCenterIds`, but the scheduler may
assign a different data center (or none); the launcher records the
**scheduler-assigned** data center from the create response, not the request.
The GPU catalog's `availability` values (`NONE`/`LOW`/`MEDIUM`/`HIGH`) are
advisory only — a low-availability GPU can still succeed, and a high one can
still fail at create time. The launcher always trusts the pod's live
`actions` list to decide whether `stop`/`start`/`terminate` are valid for its
current status (e.g. a `RUNNING` pod supports stop/restart/terminate; an
`EXITED` or `ERROR` pod supports start/terminate; a `PROVISIONING`/`STARTING`
pod supports stop/terminate). `stop` returns success and keeps the disk (cost
zero); `terminate` deletes the disk.

> Source: RunPod API documentation, retrieved 2026-08-30. The facts above are
> summarized and paraphrased; see the RunPod API reference for the current
> authoritative schema.
