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
        if self.settings.get("image_mode"):  # an image model (Klein): a jpg, sized by `resolution`
            w, h = (int(v) for v in self.settings["resolution"].split("x"))
            out = paths.wan2gp_output_dir() / f"image_{self.settings['seed']}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            if "vague" in self.settings["prompt"]:  # Klein copies its source when it does not understand
                Image.open(self.settings["image_refs"][0]).convert("RGB").resize((w, h)).save(out, "JPEG", quality=90)
            else:
                Image.new("RGB", (w, h), (90, 160, 200)).save(out, "JPEG")
        else:
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


def test_keygen_replaces_the_keyframe_and_an_older_one_can_be_used_again(conn):
    job, _ = _job(conn, n_kf=2)
    g1 = service.create_keygen(conn, job["id"], 1, "Make him smile.", sources=[0], seed=1)
    g2 = service.create_keygen(conn, job["id"], 2, "Close the eyes.", sources=[0], seed=2)  # appends keyframe 2
    w = Worker(conn, session_factory=FakeSession)
    assert w.run_next() and w.run_next()
    assert w.loaded == ("flux2_klein_4b", 4.0)
    detail = service.job_detail(conn, job["id"])
    kfs = {k["position"]: k for k in detail["keyframes"]}
    assert kfs[1]["source_keygen_id"] == g1["id"] and kfs[2]["source_keygen_id"] == g2["id"]
    assert kfs[1]["match_applied"] == 1
    assert len(detail["segments"]) == 2
    gens = {g["id"]: g for g in detail["keygens"]}
    assert gens[g1["id"]]["status"] == "done" and gens[g1["id"]]["output_url"].endswith(f"gen/{g1['id']}.png")
    start = Image.open(paths.keyframes_dir(job["id"]) / "0.png")
    assert Image.open(paths.keyframes_dir(job["id"]) / "1.png").size == start.size
    assert not list(paths.wan2gp_output_dir().glob("*.jpg"))

    g3 = service.create_keygen(conn, job["id"], 1, "Make him smile.", sources=[0], seed=3)
    assert w.run_next()
    assert service.job_detail(conn, job["id"])["keyframes"][1]["source_keygen_id"] == g3["id"]
    k = service.use_keygen(conn, g1["id"])
    assert k["source_keygen_id"] == g1["id"] and k["match_applied"] == 1
    # an upload clears the link to the keygen
    k = service.add_keyframe(conn, job["id"], 1, png_bytes(portrait()))
    assert k["source_keygen_id"] is None


def test_a_keygen_that_copies_its_source_is_flagged_and_not_installed(conn):
    job, _ = _job(conn, n_kf=2)
    before = service.job_detail(conn, job["id"])["keyframes"][1]
    g = service.create_keygen(conn, job["id"], 1, "Something vague.", sources=[0], seed=1)
    ok = service.create_keygen(conn, job["id"], 1, "Make him smile.", sources=[0], seed=2)
    w = Worker(conn, session_factory=FakeSession)
    assert w.run_next()
    detail = service.job_detail(conn, job["id"])
    gen = next(x for x in detail["keygens"] if x["id"] == g["id"])
    assert gen["status"] == "done" and gen["unchanged"] and gen["change"] == 0
    assert detail["keyframes"][1]["source_keygen_id"] == before["source_keygen_id"]  # left as it was
    assert service.use_keygen(conn, g["id"])["source_keygen_id"] == g["id"]  # the user can still use it
    assert w.run_next()
    gen = next(x for x in service.job_detail(conn, job["id"])["keygens"] if x["id"] == ok["id"])
    assert not gen["unchanged"] and gen["change"] > 0.1
    assert service.job_detail(conn, job["id"])["keyframes"][1]["source_keygen_id"] == ok["id"]


def test_keygen_whose_position_is_gone_stays_done_with_the_reason(conn):
    job, _ = _job(conn, n_kf=2)
    g = service.create_keygen(conn, job["id"], 2, "Wave.", sources=[0], seed=1)  # would append keyframe 2
    last = service.job_detail(conn, job["id"])["keyframes"][1]
    service.delete_keyframe(conn, last["id"])  # position 2 is no longer next to an existing keyframe
    Worker(conn, session_factory=FakeSession).run_next()
    row = db.row(conn.execute("SELECT * FROM keygen WHERE id = ?", (g["id"],)).fetchone())
    assert row["status"] == "done" and "Not installed" in row["error"]


def test_keygen_can_replace_the_start_image(conn):
    job, seg = _job(conn, n_kf=2)
    old_start = Image.open(paths.keyframes_dir(job["id"]) / "0.png").copy()
    t = service.create_take(conn, seg["id"], "preview")
    service.cancel(conn, "take", t["id"])
    service.match_keyframe(conn, service.job_detail(conn, job["id"])["keyframes"][1]["id"])
    g = service.create_keygen(conn, job["id"], 0, "Make it a watercolor.", sources=[0, 1], seed=1)
    Worker(conn, session_factory=FakeSession).run_next()
    kfs = {k["position"]: k for k in service.job_detail(conn, job["id"])["keyframes"]}
    assert kfs[0]["source_keygen_id"] == g["id"]
    new_start = Image.open(paths.keyframes_dir(job["id"]) / "0.png")
    assert new_start.tobytes() != old_start.tobytes()
    # the adjusted keyframe is prepared again on the new start: same size
    assert Image.open(paths.keyframes_dir(job["id"]) / "1.png").size == new_start.size
    # a take keeps its own copy of the old start
    take_start = Image.open(paths.takes_dir(job["id"]) / f"{t['id']}_start.png")
    assert take_start.tobytes() == old_start.tobytes()


def test_text_only_keygen_creates_the_start_of_an_empty_job(conn):
    job = service.create_job(conn)
    g = service.create_keygen(conn, job["id"], 0, "A watercolor fox.", aspect="square", seed=1)
    assert g["settings"]["video_prompt_type"] == "" and "image_refs" not in g["settings"]
    Worker(conn, session_factory=FakeSession).run_next()
    detail = service.job_detail(conn, job["id"])
    assert detail["keygens"][0]["change"] is None and not detail["keygens"][0]["unchanged"]
    assert [k["position"] for k in detail["keyframes"]] == [0]
    assert detail["keyframes"][0]["source_keygen_id"] == g["id"] and len(detail["segments"]) == 1


def test_a_keygen_from_before_p11_lists_the_start_as_its_source(conn):
    job, _ = _job(conn, n_kf=1)
    g = service.create_keygen(conn, job["id"], 1, "Smile.", sources=[0], seed=1)
    conn.execute("UPDATE keygen SET sources = NULL WHERE id = ?", (g["id"],))
    g = service.job_detail(conn, job["id"])["keygens"][0]
    url = f"/files/{job['id']}/keyframes/gen/{g['id']}_start.png"
    assert g["sources"] == [{"kind": "keyframe", "position": 0, "url": url}]
