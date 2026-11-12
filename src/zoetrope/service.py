"""Operations on jobs, keyframes, segments, takes and assemblies. The API is a thin layer over
these; the worker only runs takes and assemblies created here.

A keyframe is an upload (the front extracts frames from a take if the user wants one) or the
result of a keygen: Flux 2 Klein edits the start image with a text instruction, so the keyframe
keeps the start's identity, framing and background (docs/benchmark.md, "Identity match of a
keyframe"). A finished keygen replaces the keyframe at its position, technical match applied;
older keygens stay listed and `use_keygen` puts one back.
Segment i goes from keyframe i to keyframe i+1 and is kept in sync with the keyframes: internal
segments are `exact` (their end frame is sent to the model), the last one takes the job's end
mode (`exact` or `guide`; in guide mode its end keyframe only serves to judge the preview).
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from . import db, images, paths, presets
from . import queue as task_queue

WORKER_ALIVE_S = 20.0


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


def _get(conn: sqlite3.Connection, table: str, row_id: int) -> dict[str, Any]:
    r = db.row(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
    if r is None:
        raise NotFound(f"{table} {row_id} not found")
    return r


# --- jobs ---------------------------------------------------------------------------------------


def create_job(conn: sqlite3.Connection, title: str = "", preset_final: str = presets.DEFAULT_FINAL,
               end_mode: str = "exact") -> dict[str, Any]:
    _check_final_preset(preset_final)
    _check_end_mode(end_mode)
    cur = conn.execute(
        "INSERT INTO job (title, created_at, preset_final, end_mode) VALUES (?, ?, ?, ?)",
        (title, db.now(), preset_final, end_mode),
    )
    return _get(conn, "job", cur.lastrowid)


def update_job(conn: sqlite3.Connection, job_id: int, *, title: str | None = None,
               preset_final: str | None = None, end_mode: str | None = None) -> dict[str, Any]:
    _get(conn, "job", job_id)
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if preset_final is not None:
        _check_final_preset(preset_final)
        fields["preset_final"] = preset_final
    if end_mode is not None:
        _check_end_mode(end_mode)
        fields["end_mode"] = end_mode
    with db.transaction(conn):
        db.update(conn, "job", job_id, **fields)
        _sync_segments(conn, job_id)
    return _get(conn, "job", job_id)


def _check_final_preset(name: str) -> None:
    try:
        p = presets.load(name)
    except presets.PresetError as e:
        raise Invalid(str(e)) from None
    if p.tier != "final":
        raise Invalid(f"Preset {name} is not a final preset")


def _check_end_mode(mode: str) -> None:
    if mode not in ("exact", "guide"):
        raise Invalid("end_mode must be 'exact' or 'guide'")


def list_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    jobs = db.rows(conn.execute("SELECT * FROM job ORDER BY id DESC").fetchall())
    for j in jobs:
        j["status"] = job_status(conn, j["id"])
        kf = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = 0", (j["id"],)).fetchone()
        j["thumbnail_url"] = paths.to_url(kf[0]) if kf else None
    return jobs


def job_status(conn: sqlite3.Connection, job_id: int) -> str:
    """Derived: running, queued, failed (last take of a segment failed), ready (every segment has
    a retained take), or draft."""
    statuses = {r[0] for r in conn.execute(
        "SELECT t.status FROM take t JOIN segment s ON s.id = t.segment_id WHERE s.job_id = ? "
        "UNION SELECT status FROM assembly WHERE job_id = ? UNION SELECT status FROM keygen WHERE job_id = ?",
        (job_id, job_id, job_id)).fetchall()}
    if "running" in statuses:
        return "running"
    if "queued" in statuses:
        return "queued"
    segs = conn.execute("SELECT id, retained_take_id FROM segment WHERE job_id = ?", (job_id,)).fetchall()
    for s in segs:
        last = conn.execute("SELECT status FROM take WHERE segment_id = ? ORDER BY id DESC LIMIT 1", (s[0],)).fetchone()
        if last and last[0] == "failed":
            return "failed"
    if segs and all(s[1] for s in segs):
        return "ready"
    return "draft"


def job_detail(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    job = _get(conn, "job", job_id)
    job["status"] = job_status(conn, job_id)
    job["keyframes"] = [public_keyframe(k) for k in db.rows(conn.execute(
        "SELECT * FROM keyframe WHERE job_id = ? ORDER BY position", (job_id,)).fetchall())]
    segments = db.rows(conn.execute("SELECT * FROM segment WHERE job_id = ? ORDER BY idx", (job_id,)).fetchall())
    for s in segments:
        s["takes"] = [public_take(t) for t in db.rows(conn.execute(
            "SELECT * FROM take WHERE segment_id = ? ORDER BY id", (s["id"],)).fetchall())]
    job["segments"] = segments
    job["assemblies"] = [public_assembly(a) for a in db.rows(conn.execute(
        "SELECT * FROM assembly WHERE job_id = ? ORDER BY id", (job_id,)).fetchall())]
    job["keygens"] = [public_keygen(k) for k in db.rows(conn.execute(
        "SELECT * FROM keygen WHERE job_id = ? ORDER BY id", (job_id,)).fetchall())]
    return job


# --- keyframes ----------------------------------------------------------------------------------


def _open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except (UnidentifiedImageError, OSError) as e:
        raise Invalid(f"Not a readable image: {e}") from None


def _start_image(conn: sqlite3.Connection, job_id: int) -> Image.Image | None:
    r = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = 0", (job_id,)).fetchone()
    return Image.open(r[0]).convert("RGB") if r else None


def _start_params(conn: sqlite3.Connection, job_id: int) -> images.PrepParams | None:
    r = conn.execute("SELECT match_params FROM keyframe WHERE job_id = ? AND position = 0", (job_id,)).fetchone()
    return _params(r[0]) if r else None


def _params(text: str | None) -> images.PrepParams:
    params = images.PrepParams(**db.loads(text, {}))
    params.background = tuple(params.background)
    return params


def _deviations(orig: Image.Image, prepared: Image.Image, params: images.PrepParams,
                start: Image.Image) -> dict[str, Any]:
    before = images.flatten(orig, params.background, remove_fringes=False)
    return {"before": images.deviation(before, start), "after": images.deviation(prepared, start)}


def add_keyframe(conn: sqlite3.Connection, job_id: int, position: int, data: bytes,
                 source_keygen_id: int | None = None) -> dict[str, Any]:
    """Upload (or replace) the keyframe at `position`; position 0 is the start image.
    `source_keygen_id` names the keygen the image comes from, None for an upload."""
    _get(conn, "job", job_id)
    count = conn.execute("SELECT COUNT(*) FROM keyframe WHERE job_id = ?", (job_id,)).fetchone()[0]
    if position < 0 or position > count:
        raise Invalid(f"Keyframe position must be between 0 and {count}")
    orig = _open_image(data)
    d = paths.keyframes_dir(job_id)
    orig_file, file = d / f"{position}_orig.png", d / f"{position}.png"
    # Every keyframe is flattened on the start image's background colour.
    start_params = _start_params(conn, job_id)
    params = images.PrepParams(background=start_params.background) if start_params else images.PrepParams()
    start = None if position == 0 else _start_image(conn, job_id)
    prepared = images.prepare(orig, params, start)
    images.save_png(orig, orig_file)
    images.save_png(prepared, file)
    deviation = None if start is None else _deviations(orig, prepared, params, start)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO keyframe (job_id, position, orig_file, file, match_applied, match_params, deviation, "
            "created_at, source_keygen_id) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?) ON CONFLICT (job_id, position) "
            "DO UPDATE SET orig_file = excluded.orig_file, file = excluded.file, match_applied = 0, "
            "match_params = excluded.match_params, deviation = excluded.deviation, created_at = excluded.created_at, "
            "source_keygen_id = excluded.source_keygen_id",
            (job_id, position, str(orig_file), str(file), json.dumps(params.to_json()),
             json.dumps(deviation) if deviation else None, db.now(), source_keygen_id),
        )
        _sync_segments(conn, job_id)
    if position == 0:
        _reprepare_others(conn, job_id)
    return public_keyframe(db.row(conn.execute(
        "SELECT * FROM keyframe WHERE job_id = ? AND position = ?", (job_id, position)).fetchone()))


def _reprepare_others(conn: sqlite3.Connection, job_id: int) -> None:
    """After the start image changes (new upload or new background): prepare the other keyframes
    again against it, each with its own match settings and the start's background colour."""
    start = _start_image(conn, job_id)
    background = _start_params(conn, job_id).background
    for k in db.rows(conn.execute("SELECT * FROM keyframe WHERE job_id = ? AND position > 0", (job_id,)).fetchall()):
        params = _params(k["match_params"])
        params.background = background
        orig = Image.open(k["orig_file"])
        prepared = images.prepare(orig, params, start)
        images.save_png(prepared, Path(k["file"]))
        dev = _deviations(orig, prepared, params, start)
        db.update(conn, "keyframe", k["id"], match_params=json.dumps(params.to_json()), deviation=json.dumps(dev))


def match_keyframe(conn: sqlite3.Connection, keyframe_id: int, *, enabled: bool = True,
                   color_match: bool = True, remove_fringes: bool = True,
                   background: str = "#ffffff") -> dict[str, Any]:
    """Technical match of a keyframe onto the start image: same size and framing, alpha flattened
    on the same colour with fringes removed, colours matched; `enabled=False` undoes it.

    On the start image (position 0), the reference, only the background colour and the fringe
    removal apply; the other keyframes are then prepared again on that background. An off-white
    background does not cure the 14B greying pure white (docs/benchmark.md, P6)."""
    k = _get(conn, "keyframe", keyframe_id)
    try:
        bg = images.parse_color(background)
    except ValueError as e:
        raise Invalid(str(e)) from None
    if k["position"] == 0:
        params = images.PrepParams(background=bg, remove_fringes=remove_fringes)
        images.save_png(images.prepare(Image.open(k["orig_file"]), params, None), Path(k["file"]))
        with db.transaction(conn):
            db.update(conn, "keyframe", keyframe_id, match_params=json.dumps(params.to_json()))
            _reprepare_others(conn, k["job_id"])
        return public_keyframe(_get(conn, "keyframe", keyframe_id))
    params = images.PrepParams(background=bg, remove_fringes=remove_fringes,
                               fit=enabled, color_match=enabled and color_match)
    start = _start_image(conn, k["job_id"])
    orig = Image.open(k["orig_file"])
    prepared = images.prepare(orig, params, start)
    images.save_png(prepared, Path(k["file"]))
    dev = _deviations(orig, prepared, params, start)
    db.update(conn, "keyframe", keyframe_id, match_applied=int(enabled),
              match_params=json.dumps(params.to_json()), deviation=json.dumps(dev))
    return public_keyframe(_get(conn, "keyframe", keyframe_id))


def delete_keyframe(conn: sqlite3.Connection, keyframe_id: int) -> None:
    """Only the last keyframe can be removed, and only if that leaves no segment with takes."""
    k = _get(conn, "keyframe", keyframe_id)
    last = conn.execute("SELECT MAX(position) FROM keyframe WHERE job_id = ?", (k["job_id"],)).fetchone()[0]
    if k["position"] != last:
        raise Invalid("Only the last keyframe can be removed")
    with db.transaction(conn):
        conn.execute("DELETE FROM keyframe WHERE id = ?", (keyframe_id,))
        _sync_segments(conn, k["job_id"])


def public_keyframe(k: dict[str, Any]) -> dict[str, Any]:
    k = dict(k)
    k["match_params"] = db.loads(k["match_params"])
    k["deviation"] = db.loads(k["deviation"])
    k["orig_url"] = paths.to_url(k.pop("orig_file"))
    file = k.pop("file")
    k["url"] = paths.to_url(file)
    # Changes whenever the prepared image is rewritten (upload, match): the front's cache buster.
    k["version"] = Path(file).stat().st_mtime_ns if Path(file).is_file() else 0
    return k


# --- keygens: keyframes generated from the start image ------------------------------------------


def create_keygen(conn: sqlite3.Connection, job_id: int, position: int, instruction: str, *,
                  seed: int | None = None, preset: str = presets.KEYFRAME) -> dict[str, Any]:
    """Queue the generation of the keyframe at `position` (an existing one, replaced when the
    keygen finishes, or the next one) by editing the start image with `instruction`. The start
    image is snapshotted: changing it later does not change this keygen."""
    _get(conn, "job", job_id)
    count = conn.execute("SELECT COUNT(*) FROM keyframe WHERE job_id = ?", (job_id,)).fetchone()[0]
    if count == 0:
        raise Invalid("The job has no start image")
    if position < 1 or position > count:
        raise Invalid(f"A generated keyframe goes at a position between 1 and {count}")
    if not instruction.strip():
        raise Invalid("The instruction is empty")
    try:
        p = presets.load(preset)
    except presets.PresetError as e:
        raise Invalid(str(e)) from None
    if p.tier != "keyframe":
        raise Invalid(f"Preset {preset} is not a keyframe preset")
    start = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = 0", (job_id,)).fetchone()[0]
    seed = presets.new_seed() if seed is None else int(seed)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO keygen (job_id, position, instruction, preset, model_type, memory_profile, settings, seed, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 'queued', ?)",
            (job_id, position, instruction.strip(), preset, p.model_type, p.memory_profile, seed, db.now()),
        )
        gen_id = cur.lastrowid
        d = paths.keygens_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        start_copy = d / f"{gen_id}_start.png"
        shutil.copyfile(start, start_copy)
        with Image.open(start_copy) as im:
            size = im.size
        try:
            settings = presets.build_keygen_settings(p, instruction=instruction, image=start_copy,
                                                     image_size=size, seed=seed)
        except presets.PresetError as e:
            raise Invalid(str(e)) from None
        (d / f"{gen_id}.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        db.update(conn, "keygen", gen_id, settings=json.dumps(settings))
    return public_keygen(_get(conn, "keygen", gen_id))


def use_keygen(conn: sqlite3.Connection, gen_id: int) -> dict[str, Any]:
    """Make a finished keygen's image the keyframe at its position, with the technical match
    (fit and colour match on the start image, the start's background). The worker calls it when
    a keygen finishes; the user calls it to go back to an older one."""
    g = _get(conn, "keygen", gen_id)
    if g["status"] != "done" or not g["output_file"] or not Path(g["output_file"]).is_file():
        raise Invalid(f"Keygen {gen_id} has no image")
    k = add_keyframe(conn, g["job_id"], g["position"], Path(g["output_file"]).read_bytes(), source_keygen_id=gen_id)
    background = "#" + "".join(f"{c:02x}" for c in _start_params(conn, g["job_id"]).background)
    return match_keyframe(conn, k["id"], enabled=True, background=background)


def public_keygen(g: dict[str, Any]) -> dict[str, Any]:
    g = dict(g)
    g["settings"] = db.loads(g["settings"], {})
    g["durations"] = db.loads(g["durations"])
    g["output_url"] = paths.to_url(g.pop("output_file"))
    g["preview_url"] = paths.to_url(g.pop("preview_file"))
    g["log_tail"] = (g["log_tail"] or "").splitlines()
    return g


# --- segments -----------------------------------------------------------------------------------


def _sync_segments(conn: sqlite3.Connection, job_id: int) -> None:
    """Make segments follow keyframes: max(1, N-1) segments for N keyframes. Call in a transaction."""
    job = _get(conn, "job", job_id)
    n = conn.execute("SELECT COUNT(*) FROM keyframe WHERE job_id = ?", (job_id,)).fetchone()[0]
    count = 0 if n == 0 else max(1, n - 1)
    extra = conn.execute("SELECT id FROM segment WHERE job_id = ? AND idx >= ?", (job_id, count)).fetchall()
    for (sid,) in extra:
        if conn.execute("SELECT 1 FROM take WHERE segment_id = ? LIMIT 1", (sid,)).fetchone():
            raise Invalid("A segment that would disappear already has takes")
        conn.execute("DELETE FROM segment WHERE id = ?", (sid,))
    for idx in range(count):
        kf_out = idx + 1 if n > 1 else None
        end_mode = "exact" if idx < count - 1 else job["end_mode"]
        conn.execute(
            "INSERT INTO segment (job_id, idx, kf_in, kf_out, end_mode) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (job_id, idx) DO UPDATE SET kf_in = excluded.kf_in, kf_out = excluded.kf_out, "
            "end_mode = excluded.end_mode",
            (job_id, idx, idx, kf_out, end_mode),
        )


def update_segment(conn: sqlite3.Connection, segment_id: int, *, prompt: str | None = None,
                   duration_s: float | None = None) -> dict[str, Any]:
    _get(conn, "segment", segment_id)
    fields: dict[str, Any] = {}
    if prompt is not None:
        fields["prompt"] = prompt
    if duration_s is not None:
        if not 0.25 <= duration_s <= 10:
            raise Invalid("duration_s must be between 0.25 and 10 seconds")
        fields["duration_s"] = duration_s
    db.update(conn, "segment", segment_id, **fields)
    return _get(conn, "segment", segment_id)


def retain_take(conn: sqlite3.Connection, segment_id: int, take_id: int | None) -> dict[str, Any]:
    _get(conn, "segment", segment_id)
    if take_id is not None:
        t = _get(conn, "take", take_id)
        if t["segment_id"] != segment_id:
            raise Invalid("That take belongs to another segment")
        if t["status"] != "done":
            raise Invalid("Only a finished take can be retained")
    db.update(conn, "segment", segment_id, retained_take_id=take_id)
    return _get(conn, "segment", segment_id)


# --- takes --------------------------------------------------------------------------------------


def create_take(conn: sqlite3.Connection, segment_id: int, tier: str, *, seed: int | None = None,
                preset: str | None = None) -> dict[str, Any]:
    """Queue a render of a segment. The preview always starts from the segment's start image only
    (FastWan 5B has no end frame); a final sends the end keyframe too unless the segment is in
    guide mode. Inputs and settings are snapshotted in the take's folder."""
    seg = _get(conn, "segment", segment_id)
    job = _get(conn, "job", seg["job_id"])
    if tier not in ("preview", "final"):
        raise Invalid("tier must be 'preview' or 'final'")
    name = preset or (presets.PREVIEW if tier == "preview" else job["preset_final"])
    try:
        p = presets.load(name)
    except presets.PresetError as e:
        raise Invalid(str(e)) from None
    if p.tier != tier:
        raise Invalid(f"Preset {name} is a {p.tier} preset")
    if not seg["prompt"].strip():
        raise Invalid("The segment has no prompt")

    def kf_file(position: int) -> Path:
        r = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = ?",
                         (job["id"], position)).fetchone()
        if r is None:
            raise Invalid(f"Keyframe {position} is missing")
        return Path(r[0])

    start = kf_file(seg["kf_in"])
    end = None
    if tier == "final" and seg["kf_out"] is not None and seg["end_mode"] == "exact":
        end = kf_file(seg["kf_out"])
        if not p.end_frame:
            raise Invalid(f"Preset {name} takes no end frame: use the guide end mode or another preset")
    seed = presets.new_seed() if seed is None else int(seed)

    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO take (segment_id, tier, preset, model_type, memory_profile, settings, seed, fps, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, 'queued', ?)",
            (segment_id, tier, name, p.model_type, p.memory_profile, seed, p.fps, db.now()),
        )
        take_id = cur.lastrowid
        d = paths.takes_dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        start_copy = d / f"{take_id}_start.png"
        shutil.copyfile(start, start_copy)
        end_copy = None
        if end is not None:
            end_copy = d / f"{take_id}_end.png"
            shutil.copyfile(end, end_copy)
        try:
            settings = presets.build_settings(p, prompt=seg["prompt"], image_start=start_copy, image_end=end_copy,
                                              duration_s=seg["duration_s"], seed=seed)
        except presets.PresetError as e:
            raise Invalid(str(e)) from None
        (d / f"{take_id}.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        db.update(conn, "take", take_id, settings=json.dumps(settings))
    return public_take(_get(conn, "take", take_id))


def replay_take(conn: sqlite3.Connection, take_id: int) -> dict[str, Any]:
    """A new take with exactly the same settings, seed included."""
    t = _get(conn, "take", take_id)
    cur = conn.execute(
        "INSERT INTO take (segment_id, tier, preset, model_type, memory_profile, settings, seed, fps, status, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
        (t["segment_id"], t["tier"], t["preset"], t["model_type"], t["memory_profile"], t["settings"],
         t["seed"], t["fps"], db.now()),
    )
    return public_take(_get(conn, "take", cur.lastrowid))


def cancel(conn: sqlite3.Connection, table: str, row_id: int) -> dict[str, Any]:
    """Queued: cancelled at once. Running: the worker sees cancel_requested and aborts."""
    with db.transaction(conn):
        r = _get(conn, table, row_id)
        if r["status"] == "queued":
            db.update(conn, table, row_id, status="cancelled", finished_at=db.now())
        elif r["status"] == "running":
            db.update(conn, table, row_id, cancel_requested=1)
        else:
            raise Invalid(f"{table} {row_id} is already {r['status']}")
    r = _get(conn, table, row_id)
    return {"take": public_take, "keygen": public_keygen, "assembly": public_assembly}[table](r)


def get_take(conn: sqlite3.Connection, take_id: int) -> dict[str, Any]:
    t = public_take(_get(conn, "take", take_id))
    if t["status"] in ("running", "queued"):
        snap = task_queue.snapshot(conn, db.now())
        for item in snap["items"]:
            if item["kind"] == "take" and item["id"] == take_id:
                t["starts_in_s"], t["remaining_s"] = item["starts_in_s"], item["remaining_s"]
    return t


def public_take(t: dict[str, Any]) -> dict[str, Any]:
    t = dict(t)
    t["settings"] = db.loads(t["settings"], {})
    t["durations"] = db.loads(t["durations"])
    t["output_url"] = paths.to_url(t.pop("output_file"))
    t["preview_url"] = paths.to_url(t.pop("preview_file"))
    t["log_tail"] = (t["log_tail"] or "").splitlines()
    return t


# --- assemblies ---------------------------------------------------------------------------------


def create_assembly(conn: sqlite3.Connection, job_id: int, *, color_match: bool = True,
                    fps_mode: str = "native") -> dict[str, Any]:
    _get(conn, "job", job_id)
    if fps_mode not in ("native", "x2"):
        raise Invalid("fps_mode must be 'native' or 'x2'")
    segs = db.rows(conn.execute("SELECT * FROM segment WHERE job_id = ? ORDER BY idx", (job_id,)).fetchall())
    if not segs:
        raise Invalid("The job has no segment")
    take_ids = []
    for s in segs:
        if not s["retained_take_id"]:
            raise Invalid(f"Segment {s['idx']} has no retained take")
        t = _get(conn, "take", s["retained_take_id"])
        if t["status"] != "done" or not t["output_file"]:
            raise Invalid(f"The retained take of segment {s['idx']} is not finished")
        take_ids.append(t["id"])
    cur = conn.execute(
        "INSERT INTO assembly (job_id, take_ids, color_match, fps_mode, status, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        (job_id, json.dumps(take_ids), int(color_match), fps_mode, db.now()),
    )
    return public_assembly(_get(conn, "assembly", cur.lastrowid))


def public_assembly(a: dict[str, Any]) -> dict[str, Any]:
    a = dict(a)
    a["take_ids"] = db.loads(a["take_ids"], [])
    a["color_match"] = bool(a["color_match"])
    a["url"] = paths.to_url(a.pop("file"))
    return a


# --- queue and worker ---------------------------------------------------------------------------


def worker_state(conn: sqlite3.Connection) -> dict[str, Any]:
    w = db.row(conn.execute("SELECT * FROM worker WHERE id = 1").fetchone())
    if w is None:
        return {"alive": False, "heartbeat_age_s": None}
    if w["heartbeat"] is None:  # stopped cleanly
        return {**w, "alive": False, "heartbeat_age_s": None}
    age = db.now() - w["heartbeat"]
    return {**w, "alive": age < WORKER_ALIVE_S, "heartbeat_age_s": round(age, 1)}


def queue_state(conn: sqlite3.Connection) -> dict[str, Any]:
    snap = task_queue.snapshot(conn, db.now())
    for item in snap["items"]:
        r = _get(conn, item["kind"], item["id"])
        if item["kind"] == "take":
            seg = _get(conn, "segment", r["segment_id"])
            item.update(job_id=seg["job_id"], segment_idx=seg["idx"], tier=r["tier"], preset=r["preset"],
                        phase=r["phase"], step=r["step"], total_steps=r["total_steps"], progress=r["progress"])
        elif item["kind"] == "keygen":
            item.update(job_id=r["job_id"], position=r["position"], tier="keyframe", preset=r["preset"],
                        phase=r["phase"], step=r["step"], total_steps=r["total_steps"], progress=r["progress"])
        else:
            item.update(job_id=r["job_id"], phase=r["phase"])
    return {"worker": worker_state(conn), **snap}
