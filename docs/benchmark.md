# Benchmark: what this machine can render

First measured run, 2026-11-12. One clip, one configuration: a baseline, not a sweep.

## Machine

| Item     | Value                                                                  |
| -------- | ---------------------------------------------------------------------- |
| GPU      | NVIDIA GeForce GTX 1080, 8 GB (8192 MiB), Pascal, compute capability 6.1 |
| Driver   | 582.78 (reports CUDA 13.0)                                             |
| RAM      | 32 GB                                                                  |
| OS       | Windows 11 Pro, WDDM                                                   |

## Software

| Item     | Value                                                                       |
| -------- | --------------------------------------------------------------------------- |
| Tool     | Wan2GP v19.29 (commit `49f128d`), in `tools/Wan2GP/`                        |
| Python   | 3.10.9 (venv created with `uv`)                                             |
| PyTorch  | 2.7.1+cu126; cu128+ wheels have no sm_61 kernels                            |
| Attention| sdpa (no Sage/Flash on Pascal, no Triton)                                   |
| Profile  | 4 (LowRAM_LowVRAM), Wan2GP's default                                        |

Wan2GP switches to fp16 weights on its own ("GPU architecture doesn't support optimised BF16
kernels").

## Run

| Parameter       | Value                                                       |
| --------------- | ----------------------------------------------------------- |
| Model           | Wan 2.2 TI2V-5B, transformer int8 (quanto), UMT5-XXL int8   |
| Mode            | image-to-video, start image only                            |
| Input           | portrait, 480x510 PNG                                       |
| Output          | 608x640 (832x480 pixel budget, input aspect ratio kept)     |
| Clip            | 61 frames at 24 fps = **2.54 s**                            |
| Steps / CFG     | 30 / 5.0, flow shift 5, seed 42                             |

Settings file and outputs: `outputs/benchmark/` (not tracked).

## Results

| Phase                              | Time            |
| ---------------------------------- | --------------- |
| Model load + text encoding         | 1 min 29 s      |
| Denoising, 30 steps                | 25 min 53 s (**51.8 s/step**, steady) |
| VAE decode                         | ~2 min          |
| **Total, launch to saved file**    | **29 min 44 s** |

| Memory (sampled every 2 s)          | Value                                        |
| ----------------------------------- | -------------------------------------------- |
| VRAM idle (desktop)                 | 0.86 GB                                      |
| VRAM during denoising (average)     | 2.0 GB                                       |
| **VRAM peak** (VAE decode)          | **5.7 GB** of 8                              |
| System RAM idle                     | 11.3 GB                                      |
| **System RAM peak**                 | **24.1 GB** of 32 (+12.8 GB for the run)     |

Rule of thumb: **~12 min of render per second of 480p video** at 30 steps.

## Observations

- **Quality is usable.** Identity holds, the prompt's smile, blink and head tilt all happen.
  No visible drift over 2.5 s.
- **Transparent PNGs become black.** The input's alpha background was rendered as black with
  artefacts. Flatten alpha onto a solid colour before submitting: a preprocessing step for
  the backend.
- **VRAM is underused during denoising** (2 GB of 8): profile 4 streams weights from RAM.
  A less aggressive profile or `--preload` may shorten each step; untested.
- **TI2V-5B has no end-frame input in Wan2GP** (`image_prompt_types_allowed = "TSVL"`, no
  `E`). The brief's keyframe + FLF2V approach needs Wan 2.2 I2V 14B (`SEVL`) or Wan 2.1
  FLF2V 14B.
- Windows Smart App Control blocked numba's unsigned DLL; it had to be turned off.

## Recommendation

**Keep 5B as the default model for single-image clips and previews.** It fits comfortably
(5.7 GB VRAM, 24 GB RAM) and its quality on this test is good. To try next, at no download
cost: the FastWan LoRA (3 steps, built into Wan2GP), which should bring a 2.5 s clip from
30 min to a few minutes and could become the preview mode of the brief.

**14B is needed anyway for keyframe interpolation**, because 5B cannot take an end frame.
Not 14B GGUF though: Wan2GP ships GGUF presets only for LTX-2, none for Wan; it serves 14B
as int8/fp8 checkpoints, which its offloading handles with the 32 GB of RAM.
The candidate is *Wan 2.2 I2V Enhanced Lightning v2 14B* (4 steps, no CFG). Estimate, not
measured: 14B uses the Wan 2.1 VAE, so ~4x more tokens than 5B at the same resolution and
~3x more parameters, partly offset by 4 single-pass steps instead of 30 doubled by CFG:
roughly 20-40 min per 2.5 s at 480p. Download: two experts of ~14 GB each, to approve first.

**ComfyUI: not yet.** Wan2GP already provides everything the brief expects from ComfyUI:
headless runs (`wgp.py --process settings.json`), a Python API (`WanGPSession`) and an MCP
server, plus Pascal-specific memory management that ComfyUI does not have. Build the backend
against Wan2GP first; install ComfyUI only if a needed workflow (a specific FLF2V node, a
colour-match node) has no Wan2GP equivalent.
