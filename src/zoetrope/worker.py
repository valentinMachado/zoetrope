"""The render worker: one long-lived process in Wan2GP's venv holding a WanGPSession, so the
loaded model survives between takes (the 14B takes minutes to load).

It talks to the API only through the SQLite base: picks the next task (zoetrope.queue order),
writes progress at each Wan2GP event (phase, step, last preview image, log tail, phase
durations), honours cancel_requested, and keeps a heartbeat. Image creations (keygens, Flux 2
Klein: a keyframe or the start image) run like takes, in the same session: loading Klein unloads
the video model. A finished keygen's image becomes the image at its position (service.use_keygen). Assemblies run
in the same queue (CPU, plus RIFE on the GPU for fps x2). Prompt reviews load Qwen3 4B beside the
session for one reply and free it (zoetrope.prompt_review.Reviewer); when the free VRAM is short,
the session's model is released first (reloaded by the next take).

Start it detached with scripts/start-worker.ps1, stop it with scripts/stop-worker.ps1 (a stop file,
read between tasks; -Force kills it). After a crash, a take left running goes back to the queue;
on its second interruption it fails.

Run directly: tools/Wan2GP/venv/Scripts/python.exe -m zoetrope.worker (with src/ on PYTHONPATH).
"""

from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from . import assembly as assembly_mod
from . import db, paths, prompt_review, service
from . import queue as task_queue

log = logging.getLogger("zoetrope.worker")

HEARTBEAT_S = 5.0
POLL_S = 2.0
FLUSH_S = 1.0
PREVIEW_EVERY_S = 2.0
LOG_TAIL_LINES = 40
MAX_INTERRUPTIONS = 2


def stop_file() -> Path:
    return paths.outputs_dir() / "worker.stop"


def make_wan2gp_session() -> Any:
    """The real WanGPSession. Imported lazily: only this process needs torch and Wan2GP."""
    wan = paths.WAN2GP_DIR
    if str(wan) not in sys.path:
        sys.path.insert(0, str(wan))
    os.chdir(wan)  # Wan2GP resolves its config, ckpts and loras relative to its folder
    from shared.api import init

    out = paths.wan2gp_output_dir()
    out.mkdir(parents=True, exist_ok=True)
    return init(root=wan, output_dir=out, cli_args=["--attention", "sdpa"], console_output=False)


def make_reviewer() -> Any:
    """The prompt reviewer; it loads its model with Wan2GP's mmgp, hence Wan2GP on sys.path."""
    if str(paths.WAN2GP_DIR) not in sys.path:
        sys.path.insert(0, str(paths.WAN2GP_DIR))
    return prompt_review.Reviewer(paths.QWEN3_DIR)


def cuda_free_bytes() -> float:
    """VRAM this process can still use: free on the card plus what torch holds in its cache."""
    import torch

    free = torch.cuda.mem_get_info()[0]
    return float(free + torch.cuda.memory_reserved() - torch.cuda.memory_allocated())


class Worker:
    def __init__(self, conn: sqlite3.Connection, session_factory: Callable[[], Any] = make_wan2gp_session,
                 reviewer_factory: Callable[[], Any] = make_reviewer,
                 free_vram: Callable[[], float] = cuda_free_bytes):
        self.conn = conn
        self.session_factory = session_factory
        self.reviewer_factory = reviewer_factory
        self.free_vram = free_vram
        self.reviewer: Any = None
        self.session: Any = None
        self.loaded: tuple[str, float] | None = None
        self.current: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    # --- lifecycle --------------------------------------------------------------------------

    def recover(self) -> None:
        """Tasks left running by a dead worker go back to the queue, or fail on a second crash."""
        with db.transaction(self.conn):
            for table in ("take", "keygen", "assembly", "review"):
                for r in self.conn.execute(f"SELECT id, interruptions, cancel_requested FROM {table} "
                                           "WHERE status = 'running'").fetchall():
                    n = r["interruptions"] + 1
                    if r["cancel_requested"]:
                        db.update(self.conn, table, r["id"], status="cancelled", interruptions=n,
                                  finished_at=db.now())
                    elif n >= MAX_INTERRUPTIONS:
                        db.update(self.conn, table, r["id"], status="failed", interruptions=n,
                                  finished_at=db.now(), error=f"Interrupted {n} times (worker stopped or crashed)")
                    else:
                        db.update(self.conn, table, r["id"], status="queued", interruptions=n, phase=None,
                                  started_at=None)
                    log.info("recovered %s %s (interruptions=%d)", table, r["id"], n)

    def _heartbeat_loop(self) -> None:
        conn = db.connect()
        started = db.now()
        while not self._stop.is_set():
            try:
                kind, cid = self.current or (None, None)
                model, profile = self.loaded or (None, None)
                conn.execute(
                    "INSERT INTO worker (id, pid, started_at, heartbeat, loaded_model, loaded_profile, current_kind, "
                    "current_id) VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET pid = excluded.pid, "
                    "started_at = excluded.started_at, heartbeat = excluded.heartbeat, "
                    "loaded_model = excluded.loaded_model, loaded_profile = excluded.loaded_profile, "
                    "current_kind = excluded.current_kind, current_id = excluded.current_id",
                    (os.getpid(), started, db.now(), model, profile, kind, cid),
                )
            except sqlite3.Error:
                log.exception("heartbeat failed")
            self._stop.wait(HEARTBEAT_S)
        conn.execute("UPDATE worker SET heartbeat = NULL, current_kind = NULL, current_id = NULL WHERE id = 1")
        conn.close()

    def start_heartbeat(self) -> None:
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._hb_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=HEARTBEAT_S + 1)

    def run_forever(self) -> None:
        self.recover()
        self.start_heartbeat()
        log.info("worker started, pid %d", os.getpid())
        try:
            while not self._stop.is_set():
                if stop_file().exists():
                    stop_file().unlink(missing_ok=True)
                    log.info("stop file found, exiting")
                    break
                if not self.run_next():
                    time.sleep(POLL_S)
        finally:
            self.stop()

    def run_next(self) -> bool:
        """Run one task if any is queued. Returns False when the queue is empty."""
        task = task_queue.next_task(self.conn, self.loaded)
        if task is None:
            return False
        with db.transaction(self.conn):
            status = self.conn.execute(f"SELECT status FROM {task.kind} WHERE id = ?", (task.id,)).fetchone()
            if status is None or status[0] != "queued":
                return True  # cancelled meanwhile
            db.update(self.conn, task.kind, task.id, status="running", started_at=db.now(), phase="starting")
        self.current = (task.kind, task.id)
        try:
            if task.kind == "take":
                self.run_take(task.row)
            elif task.kind == "keygen":
                self.run_keygen(task.row)
            elif task.kind == "review":
                self.run_review(task.row)
            else:
                self.run_assembly(task.row)
        finally:
            self.current = None
        return True

    def _session(self) -> Any:
        if self.session is None:
            log.info("starting Wan2GP session")
            self.session = self.session_factory()
        return self.session

    # --- takes ------------------------------------------------------------------------------

    def run_take(self, take: dict[str, Any]) -> None:
        job_id = self.conn.execute("SELECT job_id FROM segment WHERE id = ?", (take["segment_id"],)).fetchone()[0]

        def store(src: Path, dest_stem: Path) -> Path:
            dest = dest_stem.with_suffix(src.suffix or ".mp4")
            shutil.move(str(src), dest)
            return dest

        self._run_model_task("take", take, paths.takes_dir(job_id), store)

    def run_keygen(self, gen: dict[str, Any]) -> None:
        """Create an image; when done, it replaces the image at its position. If that
        fails (the position no longer exists), the keygen stays done with the reason in `error`
        and the user can use it later."""

        def store(src: Path, dest_stem: Path) -> Path:
            dest = dest_stem.with_suffix(".png")
            with Image.open(src) as im:
                im.convert("RGB").save(dest, "PNG")
            src.unlink(missing_ok=True)
            return dest

        if self._run_model_task("keygen", gen, paths.keygens_dir(gen["job_id"]), store) != "done":
            return
        try:
            service.use_keygen(self.conn, gen["id"])
        except (service.Invalid, service.NotFound) as e:
            log.warning("keygen %d not installed: %s", gen["id"], e)
            db.update(self.conn, "keygen", gen["id"], error=f"Not installed as keyframe: {e}")

    def _run_model_task(self, table: str, row: dict[str, Any], folder: Path,
                        store: Callable[[Path, Path], Path]) -> str:
        """Submit a take or keygen to Wan2GP, track it, and `store` its file as folder/<id>.<ext>.
        Returns the final status."""
        row_id = row["id"]
        folder.mkdir(parents=True, exist_ok=True)
        settings = json.loads(row["settings"])
        steps = int(settings.get("num_inference_steps", 0)) or None
        group = (row["model_type"], float(row["memory_profile"]))
        tracker = _Progress(self.conn, table, row_id, folder, steps)
        errors: list[str] = []
        cancel_sent = False
        try:
            job = self._session().submit_task(settings)
            for ev in _events(job):
                if ev is not None:
                    tracker.on_event(ev)
                    if ev.kind == "error":
                        errors.append(ev.data.message)
                    if ev.kind == "progress" and ev.data.phase not in ("loading_model", None):
                        self.loaded = group  # past loading: this model and profile are in memory now
                if not cancel_sent and tracker.due_check():
                    tracker.flush()
                    if self._cancel_requested(table, row_id):
                        log.info("cancelling %s %d", table, row_id)
                        job.cancel()
                        cancel_sent = True
            result = job.result()
        except Exception as e:  # noqa: BLE001 - any failure ends the task, never the worker
            log.exception("%s %d crashed", table, row_id)
            return tracker.finish("failed", error=f"{type(e).__name__}: {e}")
        if cancel_sent or self._cancel_requested(table, row_id):
            return tracker.finish("cancelled")
        if result.success and result.generated_files:
            try:
                dest = store(Path(result.generated_files[0]), folder / str(row_id))
            except OSError as e:
                return tracker.finish("failed", error=f"Could not store the output: {e}")
            self.loaded = group
            return tracker.finish("done", output_file=str(dest))
        errors += [e.message for e in result.errors]
        return tracker.finish("failed", error="; ".join(errors) or "Wan2GP returned no file")

    def _cancel_requested(self, table: str, row_id: int) -> bool:
        r = self.conn.execute(f"SELECT cancel_requested FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return bool(r and r[0])

    # --- prompt reviews ---------------------------------------------------------------------

    def run_review(self, r: dict[str, Any]) -> None:
        rid = r["id"]
        ctx = prompt_review.Context.from_json(json.loads(r["context"]))
        try:
            if self.reviewer is None:
                self.reviewer = self.reviewer_factory()
            self._make_room(self.reviewer.VRAM_NEEDED)
            out = self.reviewer.review(r["text"], ctx, should_stop=lambda: self._cancel_requested("review", rid),
                                       on_phase=lambda p: db.update(self.conn, "review", rid, phase=p))
        except Exception as e:  # noqa: BLE001 - a failed review never stops the worker
            log.exception("review %d failed", rid)
            db.update(self.conn, "review", rid, status="failed", finished_at=db.now(), phase=None,
                      error=f"{type(e).__name__}: {e}"[:4000])
            return
        if self._cancel_requested("review", rid):
            db.update(self.conn, "review", rid, status="cancelled", finished_at=db.now(), phase=None)
            return
        durations = out.pop("durations", None)
        db.update(self.conn, "review", rid, status="done", finished_at=db.now(), phase=None,
                  result=json.dumps(out), durations=json.dumps(durations) if durations else None)
        log.info("review %d done", rid)

    def _make_room(self, needed: float) -> None:
        """Release the session's model when the free VRAM is below `needed` bytes."""
        if self.session is None or self.loaded is None:
            return
        free = self.free_vram()
        if free < needed:
            log.info("%.1f GiB of VRAM free: releasing %s", free / 2**30, self.loaded)
            self.session.close()
            self.loaded = None

    # --- assemblies -------------------------------------------------------------------------

    def run_assembly(self, a: dict[str, Any]) -> None:
        aid = a["id"]
        take_ids = json.loads(a["take_ids"])
        files = []
        for tid in take_ids:
            r = self.conn.execute("SELECT output_file FROM take WHERE id = ?", (tid,)).fetchone()
            files.append(Path(r[0]) if r and r[0] else None)
        out_dir = paths.assembly_dir(a["job_id"])
        out = out_dir / f"{aid}.mp4"

        def phase(text: str) -> None:
            db.update(self.conn, "assembly", aid, phase=text)

        try:
            if any(f is None or not f.is_file() for f in files):
                raise assembly_mod.AssemblyError("A take file is missing")
            native = out if a["fps_mode"] == "native" else out_dir / f"{aid}_native.mp4"
            assembly_mod.assemble(files, native, color_match=bool(a["color_match"]),
                                  check_cancel=lambda: self._cancel_requested("assembly", aid), on_phase=phase)
            if a["fps_mode"] == "x2":
                phase("interpolating x2 (RIFE)")
                job = self._session().submit_media_postprocessing(str(native), temporal_upsampling="rife*2")
                for _ in _events(job):
                    if self._cancel_requested("assembly", aid):
                        job.cancel()
                        job.result()
                        raise assembly_mod.Cancelled
                result = job.result()
                if not result.success or not result.generated_files:
                    raise assembly_mod.AssemblyError(
                        "RIFE failed: " + "; ".join(e.message for e in result.errors))
                shutil.move(result.generated_files[0], out)
        except assembly_mod.Cancelled:
            db.update(self.conn, "assembly", aid, status="cancelled", finished_at=db.now(), phase=None)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("assembly %d failed", aid)
            db.update(self.conn, "assembly", aid, status="failed", finished_at=db.now(),
                      error=f"{type(e).__name__}: {e}")
            return
        db.update(self.conn, "assembly", aid, status="done", finished_at=db.now(), phase=None, file=str(out))


def _events(job: Any):
    """Events of a Wan2GP job, plus None every FLUSH_S without one, until the stream closes, so
    the caller can check for cancellation during a long silent step."""
    while True:
        ev = job.events.get(timeout=FLUSH_S)
        if ev is None and job.events.closed:
            return
        yield ev


class _Progress:
    """Turns Wan2GP events into take (or keygen) rows: throttled writes, phase durations, preview
    image, log."""

    def __init__(self, conn: sqlite3.Connection, table: str, take_id: int, folder: Path, steps: int | None):
        self.conn = conn
        self.table = table
        self.take_id = take_id
        self.folder = folder
        self.steps = steps
        self.started = time.time()
        self.phase: str | None = None
        self.phase_started = self.started
        self.phases: dict[str, float] = {}
        self.step_times: list[float] = []
        self.last_step: int | None = None
        self.fields: dict[str, Any] = {}
        self.tail: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)
        self.log_file = open(folder / f"{take_id}.log", "a", encoding="utf-8")
        self.last_flush = 0.0
        self.last_check = 0.0
        self.last_preview = 0.0
        self.preview_file = folder / f"{take_id}_preview.jpg"

    def _close_phase(self, now: float) -> None:
        if self.phase is not None:
            self.phases[self.phase] = self.phases.get(self.phase, 0.0) + now - self.phase_started

    def on_event(self, ev: Any) -> None:
        now = time.time()
        if ev.kind == "progress":
            p = ev.data
            phase_changed = p.phase != self.phase
            if phase_changed:
                if self.phase == "inference" and self.steps and len(self.step_times) < self.steps:
                    self.step_times.append(round(now, 1))  # the last step ends when inference does
                self._close_phase(now)
                self.phase, self.phase_started = p.phase, now
                self.fields["phase_started_at"] = now
            if p.phase == "inference" and p.current_step is not None and p.current_step != self.last_step:
                if self.last_step is not None and p.current_step > self.last_step:
                    self.step_times.append(round(now, 1))
                self.last_step = p.current_step
            self.fields.update(phase=p.phase, raw_phase=p.raw_phase, step=p.current_step,
                               total_steps=p.total_steps, progress=p.progress)
            if phase_changed:
                self.flush()
        elif ev.kind == "preview" and ev.data.image is not None and now - self.last_preview >= PREVIEW_EVERY_S:
            self.last_preview = now
            try:
                tmp = self.preview_file.with_suffix(".tmp.jpg")
                ev.data.image.convert("RGB").save(tmp, "JPEG", quality=85)
                tmp.replace(self.preview_file)
                self.fields["preview_file"] = str(self.preview_file)
            except OSError:
                log.warning("could not write preview of %s %d", self.table, self.take_id)
        elif ev.kind == "stream":
            text = ev.data.text.rstrip()
            if text:
                self.log_file.write(text + "\n")
                # tqdm-style lines carry carriage returns: keep the last state of each line
                self.tail.append(text.split("\r")[-1][-300:])
                self.fields["log_tail"] = "\n".join(self.tail)
        elif ev.kind in ("status", "info", "error"):
            text = ev.data if isinstance(ev.data, str) else getattr(ev.data, "message", str(ev.data))
            self.log_file.write(f"[{ev.kind}] {text}\n")
            self.tail.append(f"[{ev.kind}] {text}"[-300:])
            self.fields["log_tail"] = "\n".join(self.tail)

    def due_check(self) -> bool:
        now = time.time()
        if now - self.last_check >= FLUSH_S:
            self.last_check = now
            return True
        return False

    def durations(self) -> dict[str, Any]:
        now = time.time()
        phases = dict(self.phases)
        if self.phase is not None:
            phases[self.phase] = phases.get(self.phase, 0.0) + now - self.phase_started
        return {
            "phases": {k: round(v, 1) for k, v in phases.items()},
            "steps": self.steps,
            "step_ends": [round(t - self.started, 1) for t in self.step_times],
            "total": round(now - self.started, 1),
        }

    def flush(self) -> None:
        if not self.fields:
            return
        self.fields["durations"] = json.dumps(self.durations())
        db.update(self.conn, self.table, self.take_id, **self.fields)
        self.fields = {}
        self.last_flush = time.time()
        self.log_file.flush()

    def finish(self, status: str, *, output_file: str | None = None, error: str | None = None) -> str:
        self.fields.update(status=status, finished_at=db.now())
        if output_file:
            self.fields["output_file"] = output_file
        if error:
            self.fields["error"] = error[:4000]
            self.log_file.write(f"[zoetrope] {status}: {error}\n")
        self.flush()
        self.log_file.close()
        log.info("%s %d %s", self.table, self.take_id, status)
        return status


def _single_instance_lock() -> Any:
    """Hold an exclusive lock on outputs/worker.lock for the life of the process."""
    lock_path = paths.outputs_dir() / "worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another worker is already running (outputs/worker.lock is held)")
    return fh


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    _lock = _single_instance_lock()  # noqa: F841 - kept open on purpose
    stop_file().unlink(missing_ok=True)
    # The venv's python.exe is a launcher that starts the real interpreter as a child: only the
    # worker knows its own pid, which scripts/stop-worker.ps1 -Force kills.
    (paths.outputs_dir() / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
    Worker(db.connect()).run_forever()


if __name__ == "__main__":
    main()
