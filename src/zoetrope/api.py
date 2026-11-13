"""HTTP API. Light, own venv, restartable without touching a render: it only reads and writes the
SQLite base, the worker does the rendering. The front polls it (no WebSocket).

Also serves the job files at /files and the front (src/zoetrope/front) at /.

Run: `.venv/Scripts/python -m uvicorn zoetrope.api:app --port 8765` (see scripts/start-api.ps1).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, paths, presets, service

app = FastAPI(title="Zoetrope")


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(service.NotFound)
async def _not_found(_request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.Invalid)
async def _invalid(_request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


Conn = Depends(get_conn)


# --- bodies -------------------------------------------------------------------------------------


class JobIn(BaseModel):
    title: str = ""
    preset_final: str = presets.DEFAULT_FINAL
    end_mode: str = "exact"
    style: str = ""


class JobPatch(BaseModel):
    title: str | None = None
    preset_final: str | None = None
    end_mode: str | None = None
    style: str | None = None


class MatchIn(BaseModel):
    enabled: bool = True
    color_match: bool = True
    remove_fringes: bool = True
    background: str = "#ffffff"


class SegmentPatch(BaseModel):
    prompt: str | None = None
    duration_s: float | None = None


class CopyIn(BaseModel):
    position: int | None = None


class GestureIn(BaseModel):
    instruction: str
    base: str = "end"
    back: bool = True
    prompt: str = ""
    back_prompt: str = ""
    seed: int | None = None


class TakeIn(BaseModel):
    tier: str = "preview"
    seed: int | None = None
    preset: str | None = None


class RetainIn(BaseModel):
    take_id: int | None


class PromptIn(BaseModel):
    """A prompt and where it is: a segment's motion prompt, or an image prompt with its target
    position and sources ({"keyframe": n} or {"upload": i}, as for a keygen)."""

    target: str
    text: str = ""
    segment_id: int | None = None
    duration_s: float | None = None
    position: int | None = None
    sources: list[dict[str, int]] = []

    def where(self) -> dict:
        if self.target == "motion":
            return {"segment_id": self.segment_id, "duration_s": self.duration_s}
        return {"position": self.position,
                "sources": [s["keyframe"] if "keyframe" in s else "upload" for s in self.sources]}


class AssemblyIn(BaseModel):
    color_match: bool = True
    fps_mode: str = "native"


# --- routes -------------------------------------------------------------------------------------


@app.get("/api/presets")
def list_presets():
    return [p.public() for p in presets.list_presets()]


@app.get("/api/jobs")
def list_jobs(conn=Conn):
    return service.list_jobs(conn)


@app.post("/api/jobs", status_code=201)
def create_job(body: JobIn, conn=Conn):
    job = service.create_job(conn, body.title, body.preset_final, body.end_mode, body.style)
    return service.job_detail(conn, job["id"])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, conn=Conn):
    return service.job_detail(conn, job_id)


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: int, body: JobPatch, conn=Conn):
    service.update_job(conn, job_id, **body.model_dump())
    return service.job_detail(conn, job_id)


@app.post("/api/jobs/{job_id}/keyframes", status_code=201)
async def upload_keyframe(job_id: int, position: int = Form(...), file: UploadFile = File(...),
                          insert: bool = Form(False), conn=Conn):
    """At `position`: replaces the image there, or adds one at the end; with `insert`, the images
    from `position` on move one further."""
    if position < 0:
        raise HTTPException(400, "position must be >= 0")
    if insert:
        return service.insert_keyframe(conn, job_id, position, await file.read())
    return service.add_keyframe(conn, job_id, position, await file.read())


@app.post("/api/keyframes/{keyframe_id}/copy", status_code=201)
def copy_keyframe(keyframe_id: int, body: CopyIn, conn=Conn):
    return service.copy_keyframe(conn, keyframe_id, body.position)


@app.post("/api/keyframes/{keyframe_id}/match")
def match_keyframe(keyframe_id: int, body: MatchIn, conn=Conn):
    return service.match_keyframe(conn, keyframe_id, **body.model_dump())


@app.delete("/api/keyframes/{keyframe_id}", status_code=204)
def delete_keyframe(keyframe_id: int, conn=Conn):
    service.delete_keyframe(conn, keyframe_id)


@app.post("/api/jobs/{job_id}/keygens", status_code=201)
async def create_keygen(job_id: int, position: int = Form(...), instruction: str = Form(""),
                        sources: str = Form("[]"), aspect: str | None = Form(None), seed: int | None = Form(None),
                        files: list[UploadFile] | None = File(None), conn=Conn):
    """Multipart: `sources` is a JSON list, in order, of {"keyframe": position} or {"upload": i},
    the i-th of `files`; empty for an image from the prompt alone."""
    uploads = [await f.read() for f in files or []]
    try:
        spec = json.loads(sources)
        resolved = [s["keyframe"] if "keyframe" in s else uploads[s["upload"]] for s in spec]
    except (ValueError, TypeError, KeyError, IndexError):
        raise HTTPException(400, "sources must be a JSON list of {keyframe: n} or {upload: i}") from None
    return service.create_keygen(conn, job_id, position, instruction, sources=resolved, aspect=aspect, seed=seed)


@app.post("/api/keygens/{gen_id}/again", status_code=201)
def again_keygen(gen_id: int, conn=Conn):
    return service.again_keygen(conn, gen_id)


@app.post("/api/keygens/{gen_id}/use")
def use_keygen(gen_id: int, conn=Conn):
    return service.use_keygen(conn, gen_id)


@app.post("/api/keygens/{gen_id}/cancel")
def cancel_keygen(gen_id: int, conn=Conn):
    return service.cancel(conn, "keygen", gen_id)


@app.post("/api/jobs/{job_id}/prompt-check")
def check_prompt(job_id: int, body: PromptIn, conn=Conn):
    return {"findings": service.check_prompt(conn, job_id, body.target, body.text, **body.where())}


@app.post("/api/jobs/{job_id}/reviews", status_code=201)
def create_review(job_id: int, body: PromptIn, conn=Conn):
    return service.create_review(conn, job_id, body.target, body.text, **body.where())


@app.post("/api/reviews/{review_id}/cancel")
def cancel_review(review_id: int, conn=Conn):
    return service.cancel(conn, "review", review_id)


@app.patch("/api/segments/{segment_id}")
def patch_segment(segment_id: int, body: SegmentPatch, conn=Conn):
    return service.update_segment(conn, segment_id, **body.model_dump())


@app.post("/api/segments/{segment_id}/gesture", status_code=201)
def cut_gesture(segment_id: int, body: GestureIn, conn=Conn):
    return service.cut_gesture(conn, segment_id, **body.model_dump())


@app.post("/api/segments/{segment_id}/takes", status_code=201)
def create_take(segment_id: int, body: TakeIn, conn=Conn):
    return service.create_take(conn, segment_id, body.tier, seed=body.seed, preset=body.preset)


@app.put("/api/segments/{segment_id}/retained")
def retain(segment_id: int, body: RetainIn, conn=Conn):
    return service.retain_take(conn, segment_id, body.take_id)


@app.get("/api/takes/{take_id}")
def get_take(take_id: int, conn=Conn):
    return service.get_take(conn, take_id)


@app.post("/api/takes/{take_id}/replay", status_code=201)
def replay_take(take_id: int, conn=Conn):
    return service.replay_take(conn, take_id)


@app.post("/api/takes/{take_id}/cancel")
def cancel_take(take_id: int, conn=Conn):
    return service.cancel(conn, "take", take_id)


@app.post("/api/jobs/{job_id}/assemblies", status_code=201)
def create_assembly(job_id: int, body: AssemblyIn, conn=Conn):
    return service.create_assembly(conn, job_id, color_match=body.color_match, fps_mode=body.fps_mode)


@app.post("/api/assemblies/{assembly_id}/cancel")
def cancel_assembly(assembly_id: int, conn=Conn):
    return service.cancel(conn, "assembly", assembly_id)


@app.get("/api/queue")
def get_queue(conn=Conn):
    return service.queue_state(conn)


@app.get("/api/worker")
def get_worker(conn=Conn):
    return service.worker_state(conn)


paths.jobs_dir().mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=paths.jobs_dir(), check_dir=False), name="files")
# The front: static files, no build step. Mounted last so /api and /files win.
app.mount("/", StaticFiles(directory=paths.FRONT_DIR, html=True), name="front")
