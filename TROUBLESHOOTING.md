# Troubleshooting

This is the single reference for diagnosing problems with this installer.
Other documents (README, FAQ, installation guides) link here instead of
repeating these steps.

Start with:

```bash
bash check.sh
```

It reports GPU, CUDA/PyTorch, installed MiniMax H3 models (for your current
`H3_WORKFLOWS` selection), ComfyUI-Manager, Spectrum, and free disk space —
without changing anything.

---

## CUDA out of memory

Reduce, in order of impact:

- Resolution
- Number of frames / duration
- Sampling steps

Or switch to a smaller `H3_TIER` (`light` uses far less VRAM than `max`) —
see [README.md § Model tiers](README.md#-model-tiers-h3_tier). Re-run:

```bash
bash install.sh --tier=light --only-models
```

Or use a GPU with more VRAM.

---

## A workflow says a model is missing, even though `check.sh` says it's installed

This is almost always a **tier mismatch**, not a missing file. The official
MiniMax H3 workflow JSON files ship with their diffusion/text-encoder loader
nodes pre-set to the `light` tier filenames
(`minimax_h3_fl2va_pruned_int8_convrot.safetensors`,
`minimax_h3_ref2va_pruned_int8_convrot.safetensors`,
`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`). If you installed with
`H3_TIER=balanced` or `H3_TIER=max` (the project default is `max`), those
exact filenames were never downloaded — different ones were.

Fix: open the workflow in ComfyUI and, in the model-loader node(s), pick the
file that was actually downloaded from the dropdown. You only need to do
this once per workflow file (ComfyUI remembers your choice after you save).

If the file really is missing (not just a different tier's filename),
verify the models folder:

```
ComfyUI/models/diffusion_models/
ComfyUI/models/text_encoders/
ComfyUI/models/vae/
```

and re-run:

```bash
bash install.sh --only-models
```

The installer downloads only what's missing for your current `H3_TIER` /
`H3_WORKFLOWS` selection.

---

## Hugging Face errors during model download

Verify, in order:

- You accepted the MiniMax H3 license at
  <https://huggingface.co/Comfy-Org/MiniMax-H3> with the **same account**
  as your token.
- Your `HF_TOKEN` is valid and has at least **Read** access
  (`hf auth whoami` inside the venv, or re-run `bash install.sh --only-models`
  to be prompted again).
- Your pod has internet access.

`bash install.sh --only-models` retries the download and re-checks repo
access before transferring anything.

---

## Download interrupted (network drop, pod restart, terminal disconnect)

Just re-run:

```bash
bash install.sh --only-models
```

Downloads resume from where they stopped (`curl -C -` / `hf download`
native resume) and files are validated by size (and SHA256 if configured in
`config.env`) before being accepted. See also [TMUX.md](TMUX.md) if
terminal disconnects are the recurring cause — `bootstrap.sh` already runs
everything inside a persistent tmux session by default.

---

## LoRA not visible in ComfyUI

- Restart ComfyUI (LoRAs are only scanned at startup / manager refresh).
- Confirm the file is directly inside `ComfyUI/models/loras/` (not a
  subfolder), e.g. via:

  ```bash
  bash install_lora.sh --list
  ```

---

## ComfyUI won't start

Run:

```bash
bash check.sh
```

and read the failures — it checks, in order: GPU (`nvidia-smi`), the Python
venv, PyTorch/CUDA, ComfyUI-Manager, the models required by your current
`H3_WORKFLOWS` selection, and free disk space.

If PyTorch/CUDA is the failure, see the next section.

---

## PyTorch installed but `torch.cuda.is_available()` is False

`lib/python.sh` picks a PyTorch build (`cu118`/`cu126`/`cu130`) from the
CUDA version reported by `nvidia-smi`, then installs it into the venv. If
CUDA is still unavailable afterwards:

- Re-run `bash update.sh` — it reinstalls PyTorch idempotently and prints
  the detected CUDA runtime vs. the installed build.
- Check `logs/install.log` (or `logs/update.log`) for the exact pip error.
  A common cause is a `requirements.txt` dependency silently pulling in a
  different `torch` version after the pinned install — `verify_cuda()`
  (in `lib/python.sh`) is designed to catch this and will tell you.
- If your driver reports a very new or very old CUDA version not yet in
  `PYTORCH_BUILD_TABLE` (`lib/python.sh`), set `TORCH_VERSION_OVERRIDE` and
  `TORCH_CUDA_INDEX_OVERRIDE` in `config.env` to force a specific build.

---

## Disk full during model download

Use a larger RunPod volume, or install a smaller `H3_TIER` and/or fewer
`H3_WORKFLOWS`:

```bash
bash install.sh --tier=light --workflows=t2v --only-models
```

`install.sh` estimates required space *before* downloading anything (only
for models actually missing and actually required by your selection) and
refuses to start if there isn't enough room. If a download is interrupted
by a full disk, re-running `install.sh --only-models` resumes it once space
is freed.

---

## CivitAI download failed (models or LoRAs)

Verify:

- The URL is correct and the file is still available on CivitAI.
- Your internet connection.
- If the model/LoRA requires authentication, that `CIVITAI_API_KEY` is set
  in your environment. `install_lora.sh` reports `401`/`403` explicitly as
  an authentication failure and `429` as a rate limit — both are non-retried
  on purpose, so re-running immediately won't help for those two cases.

Note: CivitAI only hosts the `light`-tier (pruned INT8) diffusion weights
for MiniMax H3 — `balanced` and `max` are only available via Hugging Face.

---

## Need more help?

Open a GitHub issue and attach:

- `logs/install.log` and/or `logs/update.log`
- The output of `bash check.sh`
