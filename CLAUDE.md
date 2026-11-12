# CLAUDE.md

Working notes for an AI assistant on this repository.

## What this is

Zoetrope: a local, personal image-to-video tool. One still image (photo, watercolour,
drawing) plus a prompt in, one video out. Never deployed. Target architecture and the
reasons behind it: [docs/brief-image-to-video.md](docs/brief-image-to-video.md) (French).

## Status

No backend or frontend code yet. Measured capacity of this machine:
[docs/benchmark.md](docs/benchmark.md).

Work is delivered one phase per session with the `phased-delivery` skill; the plan is in
`management/backlog/plan.md`.

## Layout

| Folder       | Content                                                           | Tracked |
| ------------ | ----------------------------------------------------------------- | ------- |
| `docs/`      | Brief, benchmark, design notes                                    | yes     |
| `src/`       | Zoetrope's own code (backend, worker, front)                      | yes     |
| `workflows/` | Templated workflow JSON (ComfyUI)                                 | yes     |
| `models/`    | Weights; Wan2GP's `checkpoints_paths` points here                 | no      |
| `loras/`     | LoRA files, selected by preset                                    | no      |
| `outputs/`   | Rendered clips and their inputs                                   | no      |
| `tools/`     | Third-party tools cloned locally, each with its own `venv/`       | no      |
| `management/`| Backlog and personal notes, in French, local only                 | no      |

## Machine constraints

GTX 1080 8 GB (Pascal, sm_61): no fp8, no usable bf16, fp16 at 1/64 rate, so everything
runs in fp32. PyTorch must be a **cu126** build: cu128+ wheels ship no Pascal kernels.
Wan2GP's venv is Python 3.10.9 + torch 2.7.1+cu126, created with `uv`.

## Rules

1. Everything tracked by git is in **English**, except the brief. The user writes in
   French; replies to the user are in French.
2. Ask before downloading any single file over 5 GB.
3. If a step fails twice, stop and explain the blocker instead of chaining workarounds.
