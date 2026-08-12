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

If you're on a 48 GB card (RTX A6000, RTX 6000 Ada, L40S...), OOMs on H3
Reference-to-Video are usually caused by `--highvram`, which keeps every
model in VRAM permanently instead of offloading. `COMFY_HIGHVRAM=auto`
(default, `config.env`) already avoids `--highvram` on these cards — if you
previously forced it with `COMFY_HIGHVRAM=true`, set it back to `auto` or
`false` and re-run `bash install.sh` (or `update.sh`) to regenerate the
launch flags.

Or use a GPU with more VRAM.

If OOMs happen unpredictably — the same generation succeeding once and
failing the next time at the same resolution/duration — this is usually
ComfyUI's speculative VRAM cache ("smart memory") leaving too little
headroom on a card that's right at the 48 GB line, not a real memory leak.
`COMFY_SMART_MEMORY=auto` (default, `config.env`) already adds
`--disable-smart-memory` on pods with enough host RAM
(`H3_MIN_RAM_FOR_SMART_MEMORY_GB`, 80 GB by default) — an **empirical
choice validated for the MiniMax H3 pipeline specifically**, based on
tests on an RTX A6000 (48 GB) that removed these OOMs up to 15 s / 2.0 MP,
not a general ComfyUI memory-management rule. It's a `main.py` launch
flag, so it applies to the whole ComfyUI process, not just H3 — if you run
other models/workflows in the same instance, it applies to them too,
untested by us for those cases. **This flag is not universally safe**: on
a host where RAM itself is the constrained resource (well under 32 GB),
forcing extra offload to RAM can make things worse instead of better — if
that's your setup, set `COMFY_SMART_MEMORY=false` and re-run
`bash install.sh` (or `update.sh`) to regenerate the launch flags.

---

## A workflow says a model is missing, even though `check.sh` says it's installed

This is almost always a **tier mismatch**, not a missing file. The official
MiniMax H3 workflow JSON files ship with their diffusion/text-encoder loader
nodes pre-set to the pruned FP8 scaled filenames
(`minimax_h3_fl2va_pruned_fp8_scaled.safetensors`,
`minimax_h3_ref2va_pruned_fp8_scaled.safetensors`,
`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`) — this is the `balanced`
tier since `light` switched to INT4Q (see
[README.md § Model tiers](README.md#-model-tiers-h3_tier)). If you installed
with `H3_TIER=light` or `H3_TIER=max` (the project default is `max`), those
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

**Known upstream issue (Hugging Face downloads only):** `hf download`
resume is currently unreliable on very large interrupted files — as of this
writing there's an open, unresolved bug in `huggingface_hub` where
re-running after an interruption can restart from near the beginning
instead of resuming
([huggingface_hub#4196](https://github.com/huggingface/huggingface_hub/issues/4196),
related: [xet-core#321](https://github.com/huggingface/xet-core/issues/321)).
This comes from the `hf`/`huggingface_hub` tooling itself, not from this
installer — re-running `bash install.sh --only-models` is still the correct
recovery step, just be aware it may re-download more than expected on a
large (tens-of-GB) Text Encoder/VAE file rather than a quick top-up.
CivitAI/LoRA downloads (`curl -C -`) are not affected by this.

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

Note: CivitAI only hosts the pruned INT8 ConvRot diffusion weights for
MiniMax H3, which is the `balanced` tier — `light` (INT4Q) and `max` are
only available via Hugging Face. `MODEL_SOURCE=civitai` with a tier other
than `balanced` now fails immediately with an explicit error.

---

## Need more help?

Open a GitHub issue and attach:

- `logs/install.log` and/or `logs/update.log`
- The output of `bash check.sh`
