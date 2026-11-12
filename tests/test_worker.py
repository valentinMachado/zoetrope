import json
import queue as pyqueue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from conftest import png_bytes, portrait
from zoetrope import assembly, db, paths, service
from zoetrope.worker import Worker


class FakeStream:
    def __init__(self):
        self.q = pyqueue.Queue()
        self.closed = False

    def put(self, kind, data=None):
        self.q.put(SimpleNamespace(kind=kind, data=data))

    def close(self):
        self.closed = True
        self.q.put(None)

    def get(self, timeout=None):
        try:
            return self.q.get(timeout=timeout)
        except pyqueue.Empty:
            return None


def _progress(phase, step=None, total=None):
    return SimpleNamespace(phase=phase, raw_phase=phase, current_step=step, total_steps=total, progress=50)


def _clip(path, color, n=9, size=(32, 24)):
    frames = np.zeros((n, size[1], size[0], 3), np.uint8)
    frames[...] = color
    frames[:, :, : size[0] // 2] = np.array(color) // 2
    assembly.write_video(frames, 16, path)


class FakeJob:
    """Plays Wan2GP's event sequence; with block=True, waits for cancel() mid-inference."""

    def __init__(self, settings, block=False):
        self.settings = settings
        self.events = FakeStream()
        self.cancelled = threading.Event()
        self.files = []
        self.block = block

    def run(self):
        e = self.events
        e.put("progress", _progress("loading_model"))
        e.put("progress", _progress("encoding_text"))
        steps = self.settings.get("num_inference_steps", 2)
        for i in range(steps + 1):
            e.put("progress", _progress("inference", i, steps))
            e.put("stream", SimpleNamespace(stream="stdout", text=f"step {i}"))
        e.put("preview", SimpleNamespace(image=Image.new("RGB", (16, 16), (9, 9, 9))))
        if self.block:
            self.cancelled.wait(5)
            e.close()
            return
        e.put("progress", _progress("decoding"))
        out = paths.wan2gp_output_dir() / "clip.mp4"
        _clip(out, (180, 90, 60))
        self.files = [str(out)]
        e.close()

    def cancel(self):
        self.cancelled.set()

    def result(self):
        ok = bool(self.files) and not self.cancelled.is_set()
        return SimpleNamespace(success=ok, generated_files=self.files,
                               errors=[] if ok else [SimpleNamespace(message="cancelled")])


class FakeSession:
    def __init__(self, block=False):
        self.block = block

    def submit_task(self, settings):
        job = FakeJob(settings, self.block)
        threading.Thread(target=job.run, daemon=True).start()
        return job


def _job(conn, n_kf=2):
    job = service.create_job(conn, "w")
    for pos in range(n_kf):
        service.add_keyframe(conn, job["id"], pos, png_bytes(portrait()))
    seg = service.job_detail(conn, job["id"])["segments"][0]
    service.update_segment(conn, seg["id"], prompt="smile")
    return job, seg


def _status(conn, table, row_id):
    return conn.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def test_take_runs_to_done_with_progress_and_durations(conn):
    job, seg = _job(conn)
    t = service.create_take(conn, seg["id"], "final")
    w = Worker(conn, session_factory=FakeSession)
    assert w.run_next() is True
    row = db.row(conn.execute("SELECT * FROM take WHERE id = ?", (t["id"],)).fetchone())
    assert row["status"] == "done", row["error"]
    assert Path(row["output_file"]) == paths.takes_dir(job["id"]) / f"{t['id']}.mp4"
    assert Path(row["output_file"]).is_file() and Path(row["preview_file"]).is_file()
    d = json.loads(row["durations"])
    assert set(d["phases"]) >= {"loading_model", "encoding_text", "inference", "decoding"}
    assert len(d["step_ends"]) == 4
    assert "step 4" in row["log_tail"]
    assert (paths.takes_dir(job["id"]) / f"{t['id']}.log").is_file()
    assert w.loaded == ("i2v_2_2_Enhanced_Lightning_v2", 5.0)
    assert w.run_next() is False


def test_cancel_running_take(conn):
    _, seg = _job(conn)
    t = service.create_take(conn, seg["id"], "preview")
    # the worker gets its own connection, as in production: one sqlite3 connection per thread
    w = Worker(db.connect(), session_factory=lambda: FakeSession(block=True))
    th = threading.Thread(target=w.run_next)
    th.start()
    deadline = time.time() + 5
    while _status(conn, "take", t["id"]) != "running" and time.time() < deadline:
        time.sleep(0.05)
    service.cancel(conn, "take", t["id"])
    th.join(10)
    assert _status(conn, "take", t["id"]) == "cancelled"


def test_recover_requeues_then_fails(conn):
    _, seg = _job(conn)
    t = service.create_take(conn, seg["id"], "preview")
    db.update(conn, "take", t["id"], status="running")
    w = Worker(conn, session_factory=FakeSession)
    w.recover()
    r = conn.execute("SELECT status, interruptions FROM take WHERE id = ?", (t["id"],)).fetchone()
    assert tuple(r) == ("queued", 1)
    db.update(conn, "take", t["id"], status="running")
    w.recover()
    assert _status(conn, "take", t["id"]) == "failed"


def test_assembly_color_matches_and_drops_repeated_frame(conn):
    job, _ = _job(conn, n_kf=3)
    segs = service.job_detail(conn, job["id"])["segments"]
    colors = [(200, 100, 50), (100, 150, 220)]
    for s, color in zip(segs, colors, strict=True):
        service.update_segment(conn, s["id"], prompt="p")
        t = service.create_take(conn, s["id"], "final")
        f = paths.takes_dir(job["id"]) / f"{t['id']}.mp4"
        _clip(f, color)
        db.update(conn, "take", t["id"], status="done", output_file=str(f))
        service.retain_take(conn, s["id"], t["id"])
    a = service.create_assembly(conn, job["id"], color_match=True)
    assert Worker(conn, session_factory=FakeSession).run_next()
    row = db.row(conn.execute("SELECT * FROM assembly WHERE id = ?", (a["id"],)).fetchone())
    assert row["status"] == "done", row["error"]
    info = assembly.probe(Path(row["file"]))
    assert info["frames"] == 9 + 8 and info["fps"] == 16
    frames = assembly.read_frames(Path(row["file"]))
    # the second segment was pulled toward the first one's colours
    assert abs(int(frames[-1, :, -1, 0].mean()) - 200) < 15
    assert service.job_status(conn, job["id"]) == "ready"


def test_assembly_fails_cleanly_on_missing_file(conn):
    job, seg = _job(conn, n_kf=1)
    t = service.create_take(conn, seg["id"], "final")
    db.update(conn, "take", t["id"], status="done", output_file=str(paths.takes_dir(job["id"]) / "nope.mp4"))
    service.retain_take(conn, seg["id"], t["id"])
    a = service.create_assembly(conn, job["id"])
    Worker(conn, session_factory=FakeSession).run_next()
    r = conn.execute("SELECT status, error FROM assembly WHERE id = ?", (a["id"],)).fetchone()
    assert r[0] == "failed" and "missing" in r[1]
