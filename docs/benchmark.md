# Benchmark: what this machine can render

First measured run and its follow-ups, 2026-11-12. One subject (same portrait and seed throughout), a handful of configurations: not a full sweep.

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
  A less aggressive profile does not shorten the step, only load and decode (see the next section).
- **TI2V-5B has no end-frame input in Wan2GP** (`image_prompt_types_allowed = "TSVL"`, no
  `E`). The brief's keyframe + FLF2V approach needs Wan 2.2 I2V 14B (`SEVL`) or Wan 2.1
  FLF2V 14B.
- Windows Smart App Control blocked numba's unsigned DLL; it had to be turned off.

## Speeding up the 5B (no 5B-sized download)

Same prompt, seed 42, 832x480 budget, 61 frames. The only download was the FastWan LoRA
(661 MB, fetched by Wan2GP into `tools/Wan2GP/loras/wan_5B/`). Memory sampled every 2 s as above.
Input flattened onto white (see below) for the two FastWan runs; the baseline used the raw PNG.

| Run                                  | Steps / CFG | s/step | Total (launch to file) | VRAM peak | RAM peak |
| ------------------------------------ | ----------- | ------ | ---------------------- | --------- | -------- |
| Baseline: 5B, profile 4              | 30 / 5.0    | 51.8   | 29 min 44 s            | 5.7 GB    | 24.1 GB  |
| FastWan 5B, profile 4                | 3 / 1.0     | 27.9   | 6 min 26 s             | 5.6 GB    | 23.9 GB  |
| **FastWan 5B, profile 1**            | 3 / 1.0     | 27.9   | **4 min 41 s**         | 7.0 GB    | 24.6 GB  |

- **FastWan is the big win**: 3 steps with no CFG pass, 4.6x faster than the baseline with
  profile 4 (6.4x with profile 1). A 2.5 s clip in under 5 minutes, about 1.8 min of render per
  second of video.
- **The step time does not depend on the profile** (27.9 s either way, and 51.8 s is almost exactly
  two passes of it): the 5B denoising is compute-bound on the 1080, not weight-streaming-bound.
  Profile 1 saves time only on model load and VAE decode (about 2 min), at the price of 1.4 GB more
  VRAM (7.0 of 8 GB peak: little headroom if the desktop takes more than ~0.9 GB).
- **VAE decode is now the biggest single cost** (about 3 min of the 4.7), longer than the three
  denoising steps (1 min 24 s).
- `--preload` was not run separately: profile 1 already preloads as many layers as fit.
- Not measured: a 30-step 5B run with profile 1 (the step-time result above says it would gain
  the same ~2 min of 30).

### Quality (frames 0, 20, 40, 60 of both clips)

- **Transparency**: flattening the alpha onto white removes the black/artefacted background; the
  output background stays clean white, as the prompt asks. Do it in the backend, before submission.
- **FastWan drifts more than the 30-step baseline.** The face narrows and looks older and more
  wrinkled from frame 20 on, the mouth stays closed at the smile's peak (the baseline shows teeth),
  the eyes stay shut longer, and a white shirt collar appears. Motion is smooth, no flicker.
  Identity is recognisable but not faithful: fine to judge a prompt, not to deliver.
- The two FastWan files differ by a few bytes between profile 4 and profile 1 (not compared
  frame by frame); the frames looked at are from the profile 4 clip.

### Preview setting chosen

`ti2v_2_2_fastwan`, 3 steps, guidance 1, flow shift 3, **profile 1**, input flattened on white.
Final quality keeps the 30-step 5B (or the 14B, see below).

## Keyframe interpolation: 14B I2V Lightning with an end frame

Can the brief's FLF2V segment (start frame + end frame → clip) run on the 1080? One run.

| Parameter       | Value                                                                     |
| --------------- | ------------------------------------------------------------------------- |
| Model           | `i2v_2_2_Enhanced_Lightning_v2`: Wan 2.2 I2V 14B, two experts (high noise, low noise), FP8 scaled, 14.3 GB each |
| Mode            | image-to-video, `image_prompt_type` `SE` (start + end frame)              |
| Start frame     | the portrait, alpha flattened on white, 480x510                           |
| End frame       | last frame of the FastWan clip above, resized to 480x510 (white background, same subject) |
| Output          | 608x656 (832x480 budget), 41 frames at 16 fps = **2.56 s**                |
| Steps / CFG     | 4 / 1.0 (both experts), flow shift 5, seed 42, **profile 4**              |

Settings and outputs: `outputs/p3/` (not tracked). Two Chrome tabs (Gmail, YouTube) were open
during the run: the RAM figures include them, roughly 1-2 GB.

| Phase                                         | Time                         |
| --------------------------------------------- | ---------------------------- |
| Model load (both experts + text encoder)      | ~8 min (files in OS cache from an earlier attempt; ~14 min cold, read from disk) |
| Text encoding                                 | 1 min 30 s                   |
| Denoising, 4 steps (2 high noise, 2 low noise)| 16 min 50 s (240, 221, 326, 223 s/step) |
| VAE decode                                    | ~45 s                        |
| **Total, launch to saved file**               | **27 min 36 s**              |

| Memory (sampled every 2 s)          | Value                                                  |
| ----------------------------------- | ------------------------------------------------------ |
| VRAM during denoising               | 2.8 GB                                                 |
| **VRAM peak** (VAE decode)          | **4.7 GB** of 8                                        |
| System RAM idle (with Chrome)       | 8.9 GB                                                 |
| RAM, high-noise expert              | 26.1 GB                                                |
| **RAM peak** (low-noise expert)     | **32.6 GB** of 32: saturated, spilling into the page file |

- **It works.** Pascal has no FP8 matmul; Wan2GP's PyTorch fallback dequantizes each layer on the
  fly (`[FP8] Backend: PyTorch`, no Triton) with no extra setup.
- **About 11 min of render per second of video**, the same rate as the 30-step 5B, for a 14B
  with an end frame. A step costs ~4 min: ~5 times a FastWan step, as estimated (more tokens,
  three times the parameters).
- **RAM is the limit, not VRAM.** Profile 4 keeps both experts in RAM; once the low-noise expert
  runs, RAM is full. The first low-noise step is the slowest (326 s), likely from paging. A first
  attempt was killed from outside after 22 min (no Python error, RAM at 26 GB then). Not measured:
  profile 5 (VeryLowRAM), which should trade RAM for load time.
- **Loading is slow**: 28.6 GB of weights from `E:`, 8 to 14 minutes before the first step.
  A backend that keeps the model loaded between segments (Wan2GP's `WanGPSession`) saves it.

### Quality (all 41 frames compared, frames 0-6, 10, 20, 30, 40 looked at)

- **Both ends are hit.** Frame 0 matches the start frame (mean pixel difference 1.7 on 255),
  frame 40 the end frame (7.4). No cut: the motion is continuous throughout.
- **The face morphs from one keyframe to the other**: the identity slides from the start
  portrait to the FastWan end frame's narrower, older face. The interpolation follows the end
  frame faithfully; keyframes must therefore share the same identity, since any drift in an end
  frame ends up in the clip.
- **Expression overshoots.** In between, the smile becomes a wide grin with teeth and squinted
  eyes, then closes back onto the end frame. The motion asked for (smile, eyes closing, head
  lowering) is there but exaggerated.
- **Artefacts**: the start frame's residual alpha fringes vanish at frame 1 (a visible jump on
  the background); a few dark specks appear on the white background mid-clip; the image looks
  over-sharpened, like the FastWan clip.

### Verdict

**FLF2V is feasible on the 1080 but slow**: ~28 min per 2.5 s segment at 480p, with RAM at its
limit. Fit for final renders of short keyframe segments run unattended (overnight queue),
not for iteration; previews stay on FastWan 5B. Keyframes need a clean background (no alpha
fringes) and one consistent identity.

## 14B memory profile, start-only clip, RIFE x2

Measured for the backend's final preset, in one `WanGPSession` (the API the worker uses), so the
second run found the model loaded. Same inputs, prompt and seed as the FLF run above, 41 frames
at 16 fps, 4 steps. Scripts and outputs: `outputs/p5/` (not tracked). No browser open.

| Run                                   | Load    | Text + VAE encode | Steps (s)            | Decode | **Total** |
| ------------------------------------- | ------- | ----------------- | -------------------- | ------ | --------- |
| FLF, profile 4 (P3, above)            | ~8 min  | 1 min 30 s        | 240, 221, 326, 223   | ~45 s  | 27 min 36 s |
| **FLF, profile 5**                    | **22 s**| 1 min 32 s + 26 s | 316, 219, 383, 226   | 45 s   | **22 min 10 s** |
| Start frame only, profile 5           | none (loaded) | cached + 25 s | 322, 206, 283, 227 | 41 s   | 18 min 27 s |
| RIFE x2 on the 41-frame FLF clip      | ~2 s    |                   |                      |        | **21 s**  |

- **The profile changes memory, not the result**: the profile 5 FLF clip is identical to the
  profile 4 one, pixel for pixel (mean difference 0.0).
- **Profile 5 loads in 22 s instead of 8 to 14 min**: it maps the weights and streams them from
  disk at each step instead of pinning them in RAM first. Denoising is ~2 min slower (19 min
  against 17), the first low-noise step slowest (383 s).
- **RAM is still full, but differently.** The `used` column reaches 32.5 GB of 32 again, and
  only ~250 MB stayed available during the low-noise expert, with ~60 MB/s read back from disk.
  Yet the Wan2GP process held only 6.4 GB of private memory: the rest of its 23.6 GB working set
  is file-mapped weights, which Windows can drop and re-read, where profile 4's pinned RAM is
  private and spills to the page file. Other programs suffer while it runs (a 3-second test
  suite took 50 s). VRAM peak 4.9 GB.
- **Start frame only costs the same as FLF** per step; the 18 min 27 s above skip the text
  encoding (same prompt, cached). Count ~20 min for a 2.5 s single-image clip.
- **RIFE x2 is cheap**: 21 s for 41 to 81 frames, model download (`rife4.26.pkl`, small)
  and load included. Fine as a queued step of the assembly.

Quality of the start-only clip (frames 0, 10, 20, 30, 40): identity holds far better than with
the FastWan end frame (no slide toward the narrower face), but the motion departs from the
prompt: a laugh with squeezed eyes from frame 10 on, no head lowering, and the start image's
coloured fringes and dark specks stay on the background throughout. The user's verdict on the
three 14B clips of this portrait: **the start-only one drifts least and is the best**; a FastWan
frame as end keyframe pulls the identity away.

**Choice: profile 5 for the `final-14b` preset.** Loading in seconds matters more than 2 min of
denoising, since previews (5B, profile 1) between finals force the 14B to reload, and RAM
pressure no longer goes to the page file.

### Through the backend's queue

First renders submitted through the API and run by the worker, same portrait (the original
RGBA cut-out as start image, prepared by the backend: alpha flattened on white, matte choked by
2 px), same prompt and seed 42:

- Preview (FastWan 5B, profile 1, 61 frames): 10 min 14 s, of which 5 min 38 s loading after
  the measurements above had filled RAM (P2: 4 min 41 s in all). **No jump at frame 1** any
  more: the frame 0 to 1 difference (5.3) equals the next ones (5.1), where P3's start image,
  flattened without fringe removal, jumped.
- Final (14B, profile 5, FLF to the FastWan end frame after the backend's technical match:
  fitted to the start image's size, colours histogram-matched, colour deviation 12.0 to 0.2):
  **19 min 55 s** (load 20 s, prompt encoding cached from the preview, 4 steps in 18 min 22 s,
  decode 44 s). Both ends are hit (frame 0 against the start image 1.3, frame 40 against the end
  keyframe 8.0) and the face morphs as in P3. **New defect: at frame 1 the whole white background
  turns light grey** (frame 0 to 1 difference 13.6, then ~5) and stays grey, with a thin blue line
  along the bottom edge late in the clip. The fringe jump is gone; this one is the 14B not
  holding pure white (255). Not addressed yet.
- Assembly of that take at x2 (RIFE through the worker, colour match on): 11 s, 81 frames at
  32 fps.

## Recommendation

**5B for previews.** It fits comfortably (5.7 GB VRAM, 24 GB RAM). FastWan (3 steps): 4 min
41 s per 2.5 s clip, the preview mode of the brief. The 30-step 5B stays available as an optional
final preset (`final-5b`), not the default: finals all go to the 14B so the queue never has to
switch final models.

**14B is needed anyway for keyframe interpolation**, because 5B cannot take an end frame.
Not 14B GGUF though: Wan2GP ships GGUF presets only for LTX-2, none for Wan; it serves 14B
as int8/fp8 checkpoints, which its offloading handles with the 32 GB of RAM.
*Wan 2.2 I2V Enhanced Lightning v2 14B* (4 steps, no CFG) is now measured (section above):
27 min 36 s per 2.56 s segment, RAM saturated; 22 min 10 s with profile 5 (section above).
The backend uses it for every final render, queued (preset `final-14b`).

**ComfyUI: not yet.** Wan2GP already provides everything the brief expects from ComfyUI:
headless runs (`wgp.py --process settings.json`), a Python API (`WanGPSession`) and an MCP
server, plus Pascal-specific memory management that ComfyUI does not have. Build the backend
against Wan2GP first; install ComfyUI only if a needed workflow (a specific FLF2V node, a
colour-match node) has no Wan2GP equivalent.
