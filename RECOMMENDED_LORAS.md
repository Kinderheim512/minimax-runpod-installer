# MiniMax H3 Turbo LoRA (Experimental)

**Repository**
https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora

## Description

MiniMax H3 Turbo is an experimental LoRA that significantly accelerates MiniMax H3 generation.

Instead of the standard ~20 sampling steps, it can generate synchronized video and stereo audio in as few as **4 steps**, providing up to **5× faster** sampling.

This project is currently an **early preview** and is still under active development.

## Recommended File

```
minimax_h3_turbo_4step_ckpt500.safetensors
```

## Installation

```bash
bash install_lora.sh "https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora/resolve/main/minimax_h3_turbo_4step_ckpt500.safetensors"
```

## Requirements

- MiniMax H3 **BF16**
- MiniMax H3 **INT8 ConvRot**

**Not compatible with:**

- pruned_int8
- pruned_fp8

The pruned models use a different time-conditioning layer and cannot load this LoRA.

## ComfyUI Requirements

Requires the custom node:

https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo

The official MiniMax workflow must be modified:

- Insert the Turbo LoRA between the model loader and the sampler.
- Replace the default sampler with **MiniMax H3 Turbo Sampler**.
- Use the **simple** scheduler.

## Recommended Settings

Although the LoRA is designed for **4 steps**, the author currently recommends using **6–8 steps** for noticeably sharper results.

Scheduler:

```
simple
```

## Status

⚠️ Experimental

This is an early checkpoint intended for testing. New versions are expected to improve quality over time.