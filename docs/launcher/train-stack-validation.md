# `train` stack — validation status

Companion to [train-stack.md](train-stack.md) §Validation. Records what has actually been
run, and what still needs a pod. Update it when a step is executed.

| # | Check | Status |
|---|---|---|
| — | 1587 launcher tests (includes `tests/train/`, 81 of them) | ✅ pass |
| — | Static: config, two-port tunnel, `TrainOps`, template checks, `restart_train` | ✅ pass |
| 1 | `openfox-forge start --stack train` reaches the desktop | ⬜ needs a pod |
| 2 | `TrainOps.pod_fizgig_commit()` == the pinned commit | ⬜ needs a pod |
| 3 | `git -C /workspace/Fizgig remote` is empty on the pod | ⬜ needs a pod |
| 4 | 6080/8080 unreachable publicly, reachable only via the tunnel | ⬜ needs a pod |
| 5 | `tcpdump` during a run shows no egress except the tunnel | ⬜ needs a pod |
| 6 | End to end: photos → H3 LoRA → collected → installed on `comfy` → clip | ⬜ needs a pod |

## Prerequisites before step 1

1. Build and publish the image: `.github/workflows/docker-train.yml` (manual dispatch).
2. Create the private RunPod template from `scripts/train/template.spec.json`.
   **SSH access must be enabled** — the image only starts sshd when `PUBLIC_KEY` is set,
   and without it the tunnel can never come up.
3. Store the template id: `openfox-forge credentials set` (field *RunPod Train template
   ID*) or `RUNPOD_TRAIN_TEMPLATE_ID`.
4. Verify it: `python scripts/train/verify_template.py`.
5. Set `VNC_PASSWORD` (12+ characters) in the credential store, or read the generated one
   from the pod log.
