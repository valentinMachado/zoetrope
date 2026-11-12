# CLAUDE.md

Working notes for an AI assistant on this repository.

## What this is

Zoetrope: a local, personal image-to-video tool. One still image (photo, watercolour,
drawing) plus a prompt in, one video out. Never deployed. Target architecture and the
reasons behind it: [docs/brief-image-to-video.md](docs/brief-image-to-video.md) (French).

## Status

Backend (API, render worker, presets) and web front in place. Measured capacity of this
machine: [docs/benchmark.md](docs/benchmark.md).

Work is delivered one phase per session with the `phased-delivery` skill; the plan is in
`management/backlog/plan.md`.

## Layout

| Folder       | Content                                                           | Tracked |
| ------------ | ----------------------------------------------------------------- | ------- |
| `docs/`      | Brief, benchmark, design notes                                    | yes     |
| `src/`       | Zoetrope's own code: package `zoetrope` (API, worker, front)      | yes     |
| `tests/`     | pytest suite (fake Wan2GP session, real ffmpeg)                   | yes     |
| `presets/`   | Render presets: Wan2GP settings templates, one JSON per preset    | yes     |
| `scripts/`   | PowerShell launchers for the API and the worker                   | yes     |
| `workflows/` | Templated workflow JSON (ComfyUI), unused so far                  | yes     |
| `models/`    | Weights; Wan2GP's `checkpoints_paths` points here                 | no      |
| `loras/`     | LoRA files, selected by preset                                    | no      |
| `outputs/`   | SQLite base, jobs (keyframes, takes, assemblies), worker log      | no      |
| `tools/`     | Third-party tools cloned locally, each with its own `venv/`       | no      |
| `management/`| Backlog and personal notes, in French, local only                 | no      |

## Backend

Two processes that talk only through `outputs/zoetrope.db` (SQLite, WAL):

- **API** (`zoetrope.api`, FastAPI): own venv `.venv/` (`py -m uv sync`), serves `/api/*`, the
  job files at `/files/*` and the front at `/`. Restarting it never touches a render.
- **Worker** (`zoetrope.worker`): runs in Wan2GP's venv with `src/` on `PYTHONPATH`, holds one
  `WanGPSession` so the loaded model survives between takes. Shared modules must stay
  Python 3.10-compatible and must not import FastAPI.

The front (`src/zoetrope/front/`: one HTML page, plain JS, no build step) polls the API and
only rebuilds the parts whose data changed, so a playing video or a prompt being typed survives
a refresh. Frames are extracted in the browser (canvas) and uploaded as keyframes; any image of a
job, the start image included, can also be created by Flux 2 Klein from a prompt and/or up to
three source images (job images or uploaded files): a "keygen", a task of the worker's queue,
preset `keyframe-klein`. Only an edit of the start image keeps its identity. The job page
follows the user's path in five steps and says what to do next from the job's state
(`nextStep()` in `app.js`); fine settings sit in folded « Avancé » blocks.

Queue order, take/keygen/assembly lifecycle and keyframe preparation are documented in the module
docstrings (`queue.py`, `service.py`, `worker.py`, `images.py`, `assembly.py`).

## Commands

```powershell
py -m uv sync                                   # API venv + dev tools
.venv\Scripts\python -m pytest                  # tests
.venv\Scripts\python -m ruff check src tests    # lint
scripts\start-api.ps1                           # API and front on http://127.0.0.1:8765
scripts\start-worker.ps1                        # worker, detached; log in outputs\worker.log
scripts\stop-worker.ps1 [-Force]                # stop after the current task, or kill
```

## Machine constraints

GTX 1080 8 GB (Pascal, sm_61): no fp8, no usable bf16, fp16 at 1/64 rate, so everything
runs in fp32. PyTorch must be a **cu126** build: cu128+ wheels ship no Pascal kernels.
Wan2GP's venv is Python 3.10.9 + torch 2.7.1+cu126, created with `uv`.

## Rules

1. Everything tracked by git is in **English**, except the brief and the front's user-facing
   text (labels, help, messages in `src/zoetrope/front/`), which is in **French** at the user's
   request; the front's code and comments stay in English. The user writes in French; replies
   to the user are in French.
2. Ask before downloading any single file over 5 GB.
3. If a step fails twice, stop and explain the blocker instead of chaining workarounds.
