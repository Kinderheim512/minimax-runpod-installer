# Frequently Asked Questions

For step-by-step help with a specific error, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) instead — this page only covers
"how does this work" questions.

---

## What VRAM do I need?

**8 GB minimum** (`MIN_VRAM_GB` in `config.env`), using the `light` weight
tier. Recommended tiers by VRAM:

| VRAM | Tier |
|---|---|
| 8–23 GB | `light` |
| 24–47 GB | `balanced` |
| 48 GB+ | `max` (project default) |

See [README.md § Model tiers](README.md#-model-tiers-h3_tier) for exact
file sizes. Pass `--tier=auto` to `install.sh` to pick automatically from
detected VRAM instead of using the `max` default.

---

## Does MiniMax H3 require custom nodes?

No. Native support (`MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo`)
ships in ComfyUI >= 0.30.0, and the installer always tracks ComfyUI's
default branch. `ComfyUI-VideoHelperSuite`, `rgthree-comfy`,
`ComfyUI-KJNodes`, `ComfyUI-SolAttn_triton`, and the optional `Spectrum
MiniMax H3` acceleration node are installed for convenience, not because
they're required.

---

## Which workflows are installed?

By default (`H3_WORKFLOWS=all`), all 5 official workflow files under
`workflows/`:

- `video_minimax_h3_t2v.json` — Text → Video
- `video_minimax_h3_i2v.json` — Image → Video
- `video_minimax_h3_r2v.json` — Reference → Video
- `minimaxH3T2VI2VREF2VAdvanced_v15.json` — combined advanced workflow
- `MiniMaxH3_AllInOne.json` — all three tasks in one graph

Pass `--workflows=t2v,i2v` (or any subset) to `install.sh` to only install
workflows whose required models you actually want — a workflow is skipped
if it needs a model your selection excludes. See
[README.md § Workflows](README.md#-workflows) for details.

---

## Can I use CivitAI instead of Hugging Face?

Yes, for the diffusion models only (`MODEL_SOURCE=civitai` in
`config.env`), and **only with `--tier=balanced`**. CivitAI only hosts the
pruned INT8 ConvRot diffusion weights, which is the `balanced` tier since
`light` switched to INT4Q (Hugging Face–only, see
[README.md § Model tiers](README.md#-model-tiers-h3_tier)). The text
encoder and both VAEs always come from Hugging Face regardless of
`MODEL_SOURCE`, and `light`/`max` diffusion weights are Hugging Face–only.
Selecting `MODEL_SOURCE=civitai` with `--tier=light` or `--tier=max` fails
fast with an explicit error instead of downloading the wrong file.

---

## Can I install LoRAs?

Yes:

```bash
bash install_lora.sh "https://..."
bash install_lora.sh --list
bash install_lora.sh --remove some_lora.safetensors
```

Installed into `ComfyUI/models/loras/`. Hugging Face, CivitAI, and any
direct `.safetensors` URL are supported; no authentication is required for
public files (set `CIVITAI_API_KEY` for restricted CivitAI content). See
[RECOMMENDED_LORAS.md](RECOMMENDED_LORAS.md) for a tested example.

---

## How do I update?

```bash
git pull
bash install.sh
```

Only missing or outdated components are touched; already-downloaded models
are never re-downloaded.

---

## How do I repair missing or corrupted models?

```bash
bash install.sh --only-models
```

Corrupted or incomplete files are detected (by size, and by SHA256 if
configured) and automatically removed and re-downloaded.

---

## Where are things installed?

- ComfyUI: `${INSTALL_DIR}` (default `/workspace/ComfyUI`)
- Models: `ComfyUI/models/{diffusion_models,text_encoders,vae,loras,...}`
- Workflows: `ComfyUI/user/default/workflows/`
- Logs: `logs/install.log`, `logs/update.log`, `logs/launch.log`

---

## Which Python version is used?

Whatever `python3` resolves to on the base image, as long as it's >= 3.10
(`PY_MIN_MAJOR`/`PY_MIN_MINOR` in `lib/python.sh`). Most RunPod PyTorch
templates ship 3.10–3.12.

---

## Which CUDA and PyTorch build gets installed?

Auto-detected: the installer reads the CUDA runtime reported by
`nvidia-smi` and picks a matching PyTorch build from a table in
`lib/python.sh` (currently cu118, cu126, or cu130). You can force a
specific build with `TORCH_VERSION_OVERRIDE` / `TORCH_CUDA_INDEX_OVERRIDE`
in `config.env`.

---

## Can I interrupt the installation?

Yes. Re-run `bash install.sh` — completed steps are skipped (tracked in
`.minimax_installer_state`), and model downloads resume rather than
restart. Use `--force` to redo every step regardless of prior state.

---

## Can I back up my models?

Yes — back up the whole `ComfyUI/models/` folder, or just the subfolders
you care about (`diffusion_models/`, `text_encoders/`, `vae/`, `loras/`).
`uninstall.sh` also offers to keep `models/` when removing everything else.

---

## Does tmux start automatically?

Yes, if you install via `bootstrap.sh` (the recommended path) — it ends
with `launch.sh --tmux`, which creates or reattaches to a persistent
`minimax` tmux session. See [TMUX.md](TMUX.md) for the full picture,
including how to use it manually.
