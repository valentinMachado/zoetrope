"""Operations on jobs, keyframes, segments, takes and assemblies. The API is a thin layer over
these; the worker only runs takes and assemblies created here.

A keyframe, the start image included, is an upload (the front extracts frames from a take if the
user wants one) or the result of a keygen: Flux 2 Klein creates an image from a text instruction
and/or up to three source images (keyframes of the job or uploaded images). Editing the start
image keeps its identity, framing and background (docs/benchmark.md, "Identity match of a
keyframe"); another source does not necessarily. A finished keygen replaces the image at its
position, technical match applied; older keygens stay listed and `use_keygen` puts one back.
Segment i goes from keyframe i to keyframe i+1 and is kept in sync with the keyframes: internal
segments are `exact` (their end frame is sent to the model), the last one takes the job's end
mode (`exact` or `guide`; in guide mode its end keyframe only serves to judge the preview).
A keyframe can be inserted in the middle, copied, or removed from the middle (see "Inserting and
removing in the middle" below); `cut_gesture` uses that to give a fine gesture (a wink) its own
segment towards an image of it made by Klein (P14-P15).
A prompt being written gets the instant rules of zoetrope.prompt_review (`check_prompt`) and, on
request, an AI review queued for the worker (`create_review`), given its context as text.
Jobs, takes, keygens and assemblies are deleted with their files, never while queued or running
(see "deleting" below).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from . import db, images, paths, presets, prompt_review
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
               end_mode: str = "exact", style: str = "") -> dict[str, Any]:
    _check_final_preset(preset_final)
    _check_end_mode(end_mode)
    cur = conn.execute(
        "INSERT INTO job (title, created_at, preset_final, end_mode, style) VALUES (?, ?, ?, ?, ?)",
        (title, db.now(), preset_final, end_mode, style.strip()),
    )
    return _get(conn, "job", cur.lastrowid)


def update_job(conn: sqlite3.Connection, job_id: int, *, title: str | None = None,
               preset_final: str | None = None, end_mode: str | None = None,
               style: str | None = None) -> dict[str, Any]:
    """A new style applies to the takes and images created from then on, not to past ones."""
    _get(conn, "job", job_id)
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if style is not None:
        fields["style"] = style.strip()
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
    """Every job with a summary for the projects page, the most recently active first: counts of
    images, shots, retained takes and takes, its tasks in the queue, its last finished video and its
    size on disk."""
    jobs = db.rows(conn.execute("SELECT * FROM job").fetchall())
    for j in jobs:
        jid = j["id"]
        j["status"] = job_status(conn, jid)
        kf = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = 0", (jid,)).fetchone()
        j["thumbnail_url"] = paths.to_url(kf[0]) if kf else None
        j["keyframes"] = _kf_count(conn, jid)
        j["segments"], j["retained"] = conn.execute(
            "SELECT COUNT(*), COUNT(retained_take_id) FROM segment WHERE job_id = ?", (jid,)).fetchone()
        j["takes"], j["takes_done"] = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(t.status = 'done'), 0) FROM take t JOIN segment s ON s.id = t.segment_id "
            "WHERE s.job_id = ?", (jid,)).fetchone()
        live = _live_tasks(conn, jid)
        j["running"], j["queued"] = live.count("running"), live.count("queued")
        video = conn.execute("SELECT file FROM assembly WHERE job_id = ? AND status = 'done' ORDER BY id DESC LIMIT 1",
                             (jid,)).fetchone()
        j["video_url"] = paths.to_url(video[0]) if video else None
        j["updated_at"] = _last_activity(conn, j)
        j["disk_bytes"] = _tree_size(paths.job_dir(jid))
    jobs.sort(key=lambda j: j["updated_at"], reverse=True)
    return jobs


def _last_activity(conn: sqlite3.Connection, job: dict[str, Any]) -> float:
    """The latest creation or end of anything in the job."""
    jid = job["id"]
    times = conn.execute(
        "SELECT MAX(created_at) FROM keyframe WHERE job_id = ? "
        "UNION ALL SELECT MAX(MAX(t.created_at, COALESCE(t.finished_at, 0))) FROM take t "
        "JOIN segment s ON s.id = t.segment_id WHERE s.job_id = ? "
        "UNION ALL SELECT MAX(MAX(created_at, COALESCE(finished_at, 0))) FROM keygen WHERE job_id = ? "
        "UNION ALL SELECT MAX(MAX(created_at, COALESCE(finished_at, 0))) FROM assembly WHERE job_id = ?",
        (jid, jid, jid, jid)).fetchall()
    return max([job["created_at"], *(t[0] for t in times if t[0])])


def _tree_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) if folder.is_dir() else 0


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
    job["reviews"] = [public_review(r) for r in db.rows(conn.execute(
        "SELECT * FROM review WHERE job_id = ? ORDER BY id", (job_id,)).fetchall())]
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
    count = _kf_count(conn, job_id)
    if position < 0 or position > count:
        raise Invalid(f"Keyframe position must be between 0 and {count}")
    orig = _open_image(data)
    with db.transaction(conn):
        _write_keyframe(conn, job_id, position, orig, source_keygen_id)
        _sync_segments(conn, job_id)
    if position == 0:
        _reprepare_others(conn, job_id)
    return _keyframe_at(conn, job_id, position)


def _kf_count(conn: sqlite3.Connection, job_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM keyframe WHERE job_id = ?", (job_id,)).fetchone()[0]


def _kf_row(conn: sqlite3.Connection, job_id: int, position: int) -> dict[str, Any] | None:
    return db.row(conn.execute("SELECT * FROM keyframe WHERE job_id = ? AND position = ?",
                               (job_id, position)).fetchone())


def _keyframe_at(conn: sqlite3.Connection, job_id: int, position: int) -> dict[str, Any]:
    return public_keyframe(_kf_row(conn, job_id, position))


def _kf_files(job_id: int, position: int) -> tuple[Path, Path]:
    """A keyframe's files are named after its position: (original, prepared)."""
    d = paths.keyframes_dir(job_id)
    return d / f"{position}_orig.png", d / f"{position}.png"


def _write_keyframe(conn: sqlite3.Connection, job_id: int, position: int, orig: Image.Image,
                    source_keygen_id: int | None) -> None:
    """Prepare and save the image at `position`, insert or replace its row. Segments are not
    synced: the caller does it, in its transaction."""
    orig_file, file = _kf_files(job_id, position)
    # Every keyframe is flattened on the start image's background colour.
    start_params = _start_params(conn, job_id)
    params = images.PrepParams(background=start_params.background) if start_params else images.PrepParams()
    start = None if position == 0 else _start_image(conn, job_id)
    prepared = images.prepare(orig, params, start)
    images.save_png(orig, orig_file)
    images.save_png(prepared, file)
    deviation = None if start is None else _deviations(orig, prepared, params, start)
    conn.execute(
        "INSERT INTO keyframe (job_id, position, orig_file, file, match_applied, match_params, deviation, "
        "created_at, source_keygen_id) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?) ON CONFLICT (job_id, position) "
        "DO UPDATE SET orig_file = excluded.orig_file, file = excluded.file, match_applied = 0, "
        "match_params = excluded.match_params, deviation = excluded.deviation, created_at = excluded.created_at, "
        "source_keygen_id = excluded.source_keygen_id",
        (job_id, position, str(orig_file), str(file), json.dumps(params.to_json()),
         json.dumps(deviation) if deviation else None, db.now(), source_keygen_id),
    )


# Inserting and removing in the middle. Keyframes, their files and the keygens that target them
# move by one position; segment rows move too, so that a segment cut in two by an insertion keeps
# its end image (its prompt and takes follow it) and a fresh, empty segment goes to the new image.
# The cut segment's takes stay valid when the new image is identical to its start image (a copy,
# as for a gesture that comes back): its retained take is kept; otherwise it is un-retained.
# Removal is the inverse, so that undoing an insertion gives back the segment with its takes.


def _shift_keyframes(conn: sqlite3.Connection, job_id: int, start: int, delta: int) -> None:
    """Move the keyframes at positions >= `start` by `delta` (+1 or -1), files renamed along, and
    the keygens targeting them. In a transaction; the slot moved into must be free."""
    rows = db.rows(conn.execute("SELECT id, position, orig_file, file FROM keyframe WHERE job_id = ? AND "
                                "position >= ? ORDER BY position", (job_id, start)).fetchall())
    for k in reversed(rows) if delta > 0 else rows:
        orig_file, file = _kf_files(job_id, k["position"] + delta)
        os.replace(k["orig_file"], orig_file)
        os.replace(k["file"], file)
        db.update(conn, "keyframe", k["id"], position=k["position"] + delta, orig_file=str(orig_file), file=str(file))
    conn.execute("UPDATE keygen SET position = position + ? WHERE job_id = ? AND position >= ?", (delta, job_id, start))


def _shift_segments(conn: sqlite3.Connection, job_id: int, start: int, delta: int) -> None:
    rows = conn.execute("SELECT id, idx FROM segment WHERE job_id = ? AND idx >= ? ORDER BY idx",
                        (job_id, start)).fetchall()
    for sid, idx in reversed(rows) if delta > 0 else rows:
        db.update(conn, "segment", sid, idx=idx + delta)


def _same_image(a: str | Path, b: str | Path) -> bool:
    return Path(a).is_file() and Path(b).is_file() and Path(a).read_bytes() == Path(b).read_bytes()


def _keep_retained_if(conn: sqlite3.Connection, job_id: int, idx: int, start_unchanged: bool) -> None:
    """The segment at `idx` got a new start image: un-retain its take unless the image is the same."""
    if not start_unchanged:
        conn.execute("UPDATE segment SET retained_take_id = NULL WHERE job_id = ? AND idx = ?", (job_id, idx))


def insert_keyframe(conn: sqlite3.Connection, job_id: int, position: int, data: bytes | None = None, *,
                    copy_of: int | None = None) -> dict[str, Any]:
    """Insert an image at `position` (1 to the number of keyframes; the last appends), the
    keyframes from there on moving one further: an upload (`data`), or an exact copy of the
    keyframe at `copy_of` (prepared image and match settings), e.g. for a gesture that comes back
    to the image it left."""
    _get(conn, "job", job_id)
    count = _kf_count(conn, job_id)
    if not 1 <= position <= count:
        raise Invalid(f"An image is inserted at a position between 1 and {count}")
    src, orig = None, None
    if copy_of is not None:
        src = _kf_row(conn, job_id, copy_of)
        if src is None:
            raise Invalid(f"Keyframe {copy_of} does not exist")
        src_bytes = Path(src["orig_file"]).read_bytes(), Path(src["file"]).read_bytes()
    else:
        orig = _open_image(data or b"")
    with db.transaction(conn):
        cut = position < count  # segment position-1 -> position is cut in two
        if cut:
            _shift_keyframes(conn, job_id, position, +1)
            _shift_segments(conn, job_id, position - 1, +1)
        if src is not None:
            orig_file, file = _kf_files(job_id, position)
            orig_file.parent.mkdir(parents=True, exist_ok=True)
            orig_file.write_bytes(src_bytes[0])
            file.write_bytes(src_bytes[1])
            conn.execute(
                "INSERT INTO keyframe (job_id, position, orig_file, file, match_applied, match_params, deviation, "
                "created_at, source_keygen_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, position, str(orig_file), str(file), src["match_applied"], src["match_params"],
                 src["deviation"], db.now(), src["source_keygen_id"]))
        else:
            _write_keyframe(conn, job_id, position, orig, None)
        _sync_segments(conn, job_id)
        if cut:
            _keep_retained_if(conn, job_id, position, _same_image(
                _kf_row(conn, job_id, position)["file"], _kf_row(conn, job_id, position - 1)["file"]))
    return _keyframe_at(conn, job_id, position)


def copy_keyframe(conn: sqlite3.Connection, keyframe_id: int, position: int | None = None) -> dict[str, Any]:
    """Insert an exact copy of a keyframe at `position`, by default right after it."""
    k = _get(conn, "keyframe", keyframe_id)
    return insert_keyframe(conn, k["job_id"], k["position"] + 1 if position is None else position,
                           copy_of=k["position"])


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
    """Remove a keyframe. The last one: the segment that ends on it goes, refused if it has takes.
    One in the middle: the two segments around it merge; the one leaving it stays (it keeps its
    end, its takes, and its retained take if the new start image is the same), the one arriving
    on it goes, refused if it has takes. The start image is only removed when it is alone.
    Finished keygens that made the removed image are detached (position -1); a queued or running
    one refuses the removal."""
    k = _get(conn, "keyframe", keyframe_id)
    job_id, p = k["job_id"], k["position"]
    last = conn.execute("SELECT MAX(position) FROM keyframe WHERE job_id = ?", (job_id,)).fetchone()[0]
    if p == last:
        if conn.execute("SELECT 1 FROM keygen WHERE job_id = ? AND position = ? AND status IN ('queued', 'running')",
                        (job_id, p)).fetchone():
            raise Invalid(f"An image is being created for image {p}: cancel it first")
        with db.transaction(conn):
            conn.execute("DELETE FROM keyframe WHERE id = ?", (keyframe_id,))
            conn.execute("UPDATE keygen SET position = -1 WHERE job_id = ? AND position = ?", (job_id, p))
            _sync_segments(conn, job_id)
        _remove_files([Path(k["orig_file"]), Path(k["file"])])
        return
    if p == 0:
        raise Invalid("The start image cannot be removed while other images follow it: replace it instead")
    arriving = db.row(conn.execute("SELECT * FROM segment WHERE job_id = ? AND idx = ?", (job_id, p - 1)).fetchone())
    if conn.execute("SELECT 1 FROM take WHERE segment_id = ? LIMIT 1", (arriving["id"],)).fetchone():
        raise Invalid(f"The shot that ends on image {p} already has takes")
    if conn.execute("SELECT 1 FROM keygen WHERE job_id = ? AND position = ? AND status IN ('queued', 'running')",
                    (job_id, p)).fetchone():
        raise Invalid(f"An image is being created for image {p}: cancel it first")
    same = _same_image(k["file"], _kf_row(conn, job_id, p - 1)["file"])
    with db.transaction(conn):
        conn.execute("DELETE FROM segment WHERE id = ?", (arriving["id"],))
        conn.execute("DELETE FROM keyframe WHERE id = ?", (keyframe_id,))
        conn.execute("UPDATE keygen SET position = -1 WHERE job_id = ? AND position = ?", (job_id, p))
        _shift_segments(conn, job_id, p, -1)
        _shift_keyframes(conn, job_id, p + 1, -1)
        _sync_segments(conn, job_id)
        _keep_retained_if(conn, job_id, p - 1, same)


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


# --- keygens: images created by Flux 2 Klein ----------------------------------------------------


def create_keygen(conn: sqlite3.Connection, job_id: int, position: int, instruction: str = "", *,
                  sources: list[int | bytes] = (), aspect: str | None = None, seed: int | None = None,
                  preset: str = presets.KEYFRAME) -> dict[str, Any]:
    """Queue the creation of the image at `position`: the start image (0), an existing keyframe
    (replaced when the keygen finishes) or the next one. `sources` are up to KEYGEN_SOURCES_MAX
    images, the first being the main scene the model edits: keyframe positions or uploaded images.
    With no source the image comes from the instruction alone, in the start image's format, or in
    `aspect` (portrait, square, landscape) when the job has no start image yet. Sources are
    snapshotted: changing a keyframe later does not change this keygen."""
    _get(conn, "job", job_id)
    count = conn.execute("SELECT COUNT(*) FROM keyframe WHERE job_id = ?", (job_id,)).fetchone()[0]
    if position < 0 or position > count:
        raise Invalid(f"A created image goes at a position between 0 and {count}")
    sources = list(sources)
    if len(sources) > presets.KEYGEN_SOURCES_MAX:
        raise Invalid(f"At most {presets.KEYGEN_SOURCES_MAX} source images")
    start_params = _start_params(conn, job_id)
    background = start_params.background if start_params else images.WHITE
    resolved: list[tuple[dict[str, Any], Image.Image]] = []
    for src in sources:
        if isinstance(src, (bytes, bytearray)):
            resolved.append(({"kind": "upload"}, images.flatten(_open_image(bytes(src)), background)))
        else:
            r = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = ?",
                             (job_id, int(src))).fetchone()
            if r is None:
                raise Invalid(f"Keyframe {src} does not exist")
            resolved.append(({"kind": "keyframe", "position": int(src)}, Image.open(r[0]).convert("RGB")))
    if not resolved and not instruction.strip():
        raise Invalid("Write a prompt, or give a source image")
    start = _start_image(conn, job_id)
    if resolved:
        size = resolved[0][1].size
    elif start is not None:
        size = start.size
    else:
        aspect = aspect or "portrait"
        if aspect not in presets.KEYGEN_ASPECTS:
            raise Invalid(f"aspect must be one of {', '.join(presets.KEYGEN_ASPECTS)}")
        size = presets.KEYGEN_ASPECTS[aspect]
    return _queue_keygen(conn, job_id, position, instruction, resolved, size, seed, preset)


def _job_style(conn: sqlite3.Connection, job_id: int) -> str:
    return _get(conn, "job", job_id)["style"] or ""


def _queue_keygen(conn: sqlite3.Connection, job_id: int, position: int, instruction: str,
                  sources: list[tuple[dict[str, Any], Image.Image]], size: tuple[int, int],
                  seed: int | None, preset: str) -> dict[str, Any]:
    try:
        p = presets.load(preset)
    except presets.PresetError as e:
        raise Invalid(str(e)) from None
    if p.tier != "keyframe":
        raise Invalid(f"Preset {preset} is not a keyframe preset")
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
        files, meta = [], []
        for i, (m, img) in enumerate(sources):
            f = d / f"{gen_id}_src{i}.png"
            images.save_png(img, f)
            files.append(f)
            meta.append({**m, "file": str(f)})
        try:
            settings = presets.build_keygen_settings(p, instruction=instruction, images=files, image_size=size,
                                                     seed=seed, style=_job_style(conn, job_id))
        except presets.PresetError as e:
            raise Invalid(str(e)) from None
        (d / f"{gen_id}.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        db.update(conn, "keygen", gen_id, settings=json.dumps(settings), sources=json.dumps(meta))
    return public_keygen(_get(conn, "keygen", gen_id))


def again_keygen(conn: sqlite3.Connection, gen_id: int) -> dict[str, Any]:
    """Another draw of a keygen: same target, prompt and source images (its snapshots), new seed."""
    g = _get(conn, "keygen", gen_id)
    sources = []
    for m in _keygen_sources(g):
        if not Path(m["file"]).is_file():
            raise Invalid(f"A source image of keygen {gen_id} is missing")
        sources.append(({k: v for k, v in m.items() if k != "file"}, Image.open(m["file"]).convert("RGB")))
    w, h = (int(v) for v in db.loads(g["settings"], {}).get("resolution", "480x512").split("x"))
    return _queue_keygen(conn, g["job_id"], g["position"], g["instruction"], sources, (w, h), None, g["preset"])


def _keygen_sources(g: dict[str, Any]) -> list[dict[str, Any]]:
    """The keygen's snapshotted sources. A keygen from before P11 had one: the start image."""
    if g["sources"] is not None:
        return db.loads(g["sources"], [])
    return [{"kind": "keyframe", "position": 0,
             "file": str(paths.keygens_dir(g["job_id"]) / f"{g['id']}_start.png")}]


def use_keygen(conn: sqlite3.Connection, gen_id: int) -> dict[str, Any]:
    """Make a finished keygen's image the keyframe at its position. The worker calls it when a
    keygen finishes; the user calls it to go back to an older one. A keyframe gets the technical
    match (fit and colour match on the start image, the start's background). A new start image
    keeps the old start's background and the other keyframes are prepared again on it; takes
    already rendered keep their copy of the old start."""
    g = _get(conn, "keygen", gen_id)
    if g["status"] != "done" or not g["output_file"] or not Path(g["output_file"]).is_file():
        raise Invalid(f"Keygen {gen_id} has no image")
    if g["position"] < 0:
        raise Invalid(f"The image keygen {gen_id} was made for has been removed")
    k = add_keyframe(conn, g["job_id"], g["position"], Path(g["output_file"]).read_bytes(), source_keygen_id=gen_id)
    if g["position"] == 0:
        return k
    background = "#" + "".join(f"{c:02x}" for c in _start_params(conn, g["job_id"]).background)
    return match_keyframe(conn, k["id"], enabled=True, background=background)


def measure_keygen_change(conn: sqlite3.Connection, gen_id: int) -> float | None:
    """Store how much a finished keygen's image changed from its main source (images.changed_share);
    None for an image from text alone. Under images.UNCHANGED_BELOW the model copied its source,
    which it does when it does not understand the instruction: the worker then leaves it out of
    the keyframes and the front says so."""
    g = _get(conn, "keygen", gen_id)
    sources = _keygen_sources(g)
    if not g["output_file"] or not sources or not Path(sources[0]["file"]).is_file():
        return None
    with Image.open(g["output_file"]) as out, Image.open(sources[0]["file"]) as src:
        change = round(images.changed_share(out, src), 4)
    db.update(conn, "keygen", gen_id, change=change)
    return change


def keygen_unchanged(g: dict[str, Any]) -> bool:
    return g.get("change") is not None and g["change"] < images.UNCHANGED_BELOW


def public_keygen(g: dict[str, Any]) -> dict[str, Any]:
    g = dict(g)
    g["unchanged"] = keygen_unchanged(g)
    g["sources"] = [{**{k: v for k, v in m.items() if k != "file"}, "url": paths.to_url(m["file"])}
                    for m in _keygen_sources(g)]
    g["settings"] = db.loads(g["settings"], {})
    g["durations"] = db.loads(g["durations"])
    g["output_url"] = paths.to_url(g.pop("output_file"))
    g["preview_url"] = paths.to_url(g.pop("preview_file"))
    g["log_tail"] = (g["log_tail"] or "").splitlines()
    return g


# --- prompt reviews (zoetrope.prompt_review) ----------------------------------------------------


def _created_from(conn: sqlite3.Connection, job_id: int, position: int) -> str | None:
    """The prompt the keyframe at `position` was created with, if a keygen made it."""
    r = conn.execute("SELECT g.instruction FROM keyframe k JOIN keygen g ON g.id = k.source_keygen_id "
                     "WHERE k.job_id = ? AND k.position = ?", (job_id, position)).fetchone()
    return r[0] if r and r[0] else None


def review_context(conn: sqlite3.Connection, job_id: int, target: str, *, segment_id: int | None = None,
                   position: int | None = None, sources: list[int | str] = (),
                   duration_s: float | None = None) -> prompt_review.Context:
    """What the review knows besides the text. Motion: the segment's duration (or `duration_s`,
    not saved yet) and end image. Image: the target position and the kinds of its `sources`
    (keyframe positions, or "upload"). Both: the prompts the images involved were created with,
    which often name their style and subject (the AI does not see the images). The job's style,
    which the render puts in front of a motion prompt or of an image from text alone."""
    style = _job_style(conn, job_id) or None
    if target == "motion":
        if segment_id is None:
            raise Invalid("A motion prompt review needs its segment")
        seg = _get(conn, "segment", segment_id)
        if seg["job_id"] != job_id:
            raise Invalid("That segment belongs to another job")
        end = None if seg["kf_out"] is None else seg["end_mode"]
        notes = []
        made_in = _created_from(conn, job_id, seg["kf_in"])
        if made_in:
            notes.append("The start image of this shot was created by an image model from this prompt "
                         f"(it tells its style and subject): \"{made_in}\"")
        made_out = _created_from(conn, job_id, seg["kf_out"]) if seg["kf_out"] is not None else None
        if made_out:
            notes.append(f"The end image was created from this prompt: \"{made_out}\"")
        return prompt_review.Context("motion", duration_s=duration_s or seg["duration_s"], end_image=end,
                                     notes=tuple(notes), style=style, start_made_from=made_in,
                                     end_made_from=made_out)
    if target != "image":
        raise Invalid(f"target must be one of {', '.join(prompt_review.TARGETS)}")
    kinds, notes = [], []
    for src in sources:
        if src == "upload":
            kinds.append("upload")
            continue
        kinds.append("start" if int(src) == 0 else "keyframe")
        made = _created_from(conn, job_id, int(src))
        if made:
            notes.append(f"Source image {len(kinds)} was created from this prompt: \"{made}\"")
    if not sources and position != 0:
        made = _created_from(conn, job_id, 0)
        if made:
            notes.append(f"The start image of the video was created from this prompt: \"{made}\"")
    return prompt_review.Context("image", position=position if position is not None else 0,
                                 sources=tuple(kinds), notes=tuple(notes), style=None if kinds else style)


def check_prompt(conn: sqlite3.Connection, job_id: int, target: str, text: str, **where: Any) -> list[dict[str, Any]]:
    """The instant rules on a prompt being written; `where` as for review_context."""
    return prompt_review.check(text, review_context(conn, job_id, target, **where))


def create_review(conn: sqlite3.Connection, job_id: int, target: str, text: str, **where: Any) -> dict[str, Any]:
    """Queue the AI review of a prompt; context and rule findings are snapshotted."""
    ctx = review_context(conn, job_id, target, **where)
    if not text.strip():
        raise Invalid("Write a prompt first")
    cur = conn.execute(
        "INSERT INTO review (job_id, target, segment_id, position, text, context, findings, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
        (job_id, target, where.get("segment_id"), ctx.position, text.strip(), json.dumps(ctx.to_json()),
         json.dumps(prompt_review.check(text, ctx)), db.now()),
    )
    return public_review(_get(conn, "review", cur.lastrowid))


def public_review(r: dict[str, Any]) -> dict[str, Any]:
    r = dict(r)
    for key, default in (("context", {}), ("findings", []), ("result", None), ("durations", None)):
        r[key] = db.loads(r[key], default)
    return r


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


def cut_gesture(conn: sqlite3.Connection, segment_id: int, instruction: str, *, base: str = "end",
                back: bool = True, prompt: str = "", back_prompt: str = "", seed: int | None = None) -> dict[str, Any]:
    """Give a fine gesture (a wink) its own segment, the way P14 got one (docs/benchmark.md,
    "Following a fine motion prompt"): Klein makes the image of the gesture from the segment's
    start or end image b (`base`), which goes right after b; with `back`, a copy of b follows it,
    so the gesture is undone in a second segment. Until the keygen finishes, the gesture's slot
    holds a copy of b, which the keygen's image replaces. The return copy is inserted first: the
    segment after b, if any, then starts from an identical image and keeps its takes. `prompt` and
    `back_prompt` go to the segments b -> gesture and gesture -> copy. Returns the keygen."""
    seg = _get(conn, "segment", segment_id)
    job_id = seg["job_id"]
    if base not in ("start", "end"):
        raise Invalid("base must be 'start' or 'end'")
    b = seg["kf_in"] if base == "start" else seg["kf_out"]
    if b is None:
        raise Invalid("This shot has no end image: make the gesture from its start image")
    if not instruction.strip():
        raise Invalid("Say what the gesture is (the instruction for the image)")
    if back:
        insert_keyframe(conn, job_id, b + 1, copy_of=b)
    insert_keyframe(conn, job_id, b + 1, copy_of=b)
    gen = create_keygen(conn, job_id, b + 1, instruction, sources=[b], seed=seed)
    with db.transaction(conn):
        # The gesture's slot is about to change: no take made before is retained on either side.
        conn.execute("UPDATE segment SET retained_take_id = NULL WHERE job_id = ? AND (kf_in = ? OR kf_out = ?)",
                     (job_id, b + 1, b + 1))
        for idx, text in ((b, prompt), (b + 1, back_prompt if back else "")):
            if text.strip():
                conn.execute("UPDATE segment SET prompt = ? WHERE job_id = ? AND idx = ?", (text.strip(), job_id, idx))
    return gen


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
                                              duration_s=seg["duration_s"], seed=seed, style=job["style"])
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
    public = {"take": public_take, "keygen": public_keygen, "assembly": public_assembly, "review": public_review}
    return public[table](r)


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


# --- deleting -----------------------------------------------------------------------------------
#
# Rows and their files go together. Nothing queued or running is deleted (cancel it first): the
# worker would write into a folder that is gone. A take is deleted even when it is retained (the
# front asks first): its shot then has no retained take. An image created by Klein is kept while it
# is the current image at its position. Each deletion returns the bytes freed on disk.


def _live_tasks(conn: sqlite3.Connection, job_id: int) -> list[str]:
    """Statuses of the job's queued or running tasks."""
    return [r[0] for r in conn.execute(
        "SELECT t.status FROM take t JOIN segment s ON s.id = t.segment_id WHERE s.job_id = ? "
        "AND t.status IN ('queued', 'running') "
        "UNION ALL SELECT status FROM keygen WHERE job_id = ? AND status IN ('queued', 'running') "
        "UNION ALL SELECT status FROM assembly WHERE job_id = ? AND status IN ('queued', 'running') "
        "UNION ALL SELECT status FROM review WHERE job_id = ? AND status IN ('queued', 'running')",
        (job_id, job_id, job_id, job_id)).fetchall()]


def _remove_files(files: list[Path]) -> int:
    """Delete the files that exist; one held open by a reader (Windows) stays. Bytes freed."""
    freed = 0
    for f in files:
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError:
            pass
    return freed


def _task_files(folder: Path, row_id: int) -> list[Path]:
    """A task's files in its folder: `<id>.<ext>` and `<id>_<name>` (inputs, preview, log)."""
    return [*folder.glob(f"{row_id}.*"), *folder.glob(f"{row_id}_*")]


def _refuse_live(r: dict[str, Any], what: str) -> None:
    if r["status"] in ("queued", "running"):
        raise Invalid(f"{what} is {r['status']}: cancel it first")


def delete_job(conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    _get(conn, "job", job_id)
    if _live_tasks(conn, job_id):
        raise Invalid(f"Job {job_id} has tasks queued or running: cancel them first")
    folder = paths.job_dir(job_id)
    size = _tree_size(folder)
    conn.execute("DELETE FROM job WHERE id = ?", (job_id,))  # the rest goes by ON DELETE CASCADE
    shutil.rmtree(folder, ignore_errors=True)
    return {"freed_bytes": size - _tree_size(folder)}


def delete_take(conn: sqlite3.Connection, take_id: int) -> dict[str, int]:
    t = _get(conn, "take", take_id)
    _refuse_live(t, f"Take {take_id}")
    seg = _get(conn, "segment", t["segment_id"])
    for (ids,) in conn.execute("SELECT take_ids FROM assembly WHERE job_id = ? AND status IN ('queued', 'running')",
                               (seg["job_id"],)).fetchall():
        if take_id in db.loads(ids, []):
            raise Invalid(f"Take {take_id} is being assembled: cancel the assembly first")
    with db.transaction(conn):
        conn.execute("UPDATE segment SET retained_take_id = NULL WHERE retained_take_id = ?", (take_id,))
        conn.execute("DELETE FROM take WHERE id = ?", (take_id,))
    # A replayed take reads its inputs from the take it copied: those stay while another take uses them.
    used = {v for (st,) in conn.execute("SELECT t.settings FROM take t JOIN segment s ON s.id = t.segment_id "
                                        "WHERE s.job_id = ?", (seg["job_id"],)).fetchall()
            for v in db.loads(st, {}).values() if isinstance(v, str)}
    files = _task_files(paths.takes_dir(seg["job_id"]), take_id)
    return {"freed_bytes": _remove_files([f for f in files if str(f) not in used])}


def delete_keygen(conn: sqlite3.Connection, gen_id: int) -> dict[str, int]:
    g = _get(conn, "keygen", gen_id)
    _refuse_live(g, f"Keygen {gen_id}")
    if conn.execute("SELECT 1 FROM keyframe WHERE source_keygen_id = ?", (gen_id,)).fetchone():
        raise Invalid(f"Keygen {gen_id} made the current image at position {g['position']}: replace it first")
    conn.execute("DELETE FROM keygen WHERE id = ?", (gen_id,))
    return {"freed_bytes": _remove_files(_task_files(paths.keygens_dir(g["job_id"]), gen_id))}


def delete_assembly(conn: sqlite3.Connection, assembly_id: int) -> dict[str, int]:
    a = _get(conn, "assembly", assembly_id)
    _refuse_live(a, f"Assembly {assembly_id}")
    conn.execute("DELETE FROM assembly WHERE id = ?", (assembly_id,))
    return {"freed_bytes": _remove_files(_task_files(paths.assembly_dir(a["job_id"]), assembly_id))}


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
        elif item["kind"] == "review":
            idx = None
            if r["segment_id"] is not None:
                idx = conn.execute("SELECT idx FROM segment WHERE id = ?", (r["segment_id"],)).fetchone()[0]
            item.update(job_id=r["job_id"], target=r["target"], segment_idx=idx, phase=r["phase"])
        else:
            item.update(job_id=r["job_id"], phase=r["phase"])
        item.update(_task_card(conn, item["kind"], r, item["job_id"]))
    return {"worker": worker_state(conn), **snap, "recent": recent_tasks(conn)}


RECENT_TASKS = 10


def _task_card(conn: sqlite3.Connection, kind: str, r: dict[str, Any], job_id: int) -> dict[str, Any]:
    """What the queue panel shows of a task besides its state: the job's title and a thumbnail
    (the take's start image, the keygen's first source, else the job's start image)."""
    title = conn.execute("SELECT title FROM job WHERE id = ?", (job_id,)).fetchone()
    thumb = None
    if kind == "take":
        thumb = db.loads(r["settings"], {}).get("image_start")
    elif kind == "keygen":
        thumb = r["output_file"] or next((s["file"] for s in _keygen_sources(r)), None)
    if not thumb or not Path(thumb).is_file():
        kf = conn.execute("SELECT file FROM keyframe WHERE job_id = ? AND position = 0", (job_id,)).fetchone()
        thumb = kf[0] if kf else None
    return {"job_title": title[0] if title else "", "thumb_url": paths.to_url(thumb) if thumb else None}


def recent_tasks(conn: sqlite3.Connection, limit: int = RECENT_TASKS) -> list[dict[str, Any]]:
    """The last takes, keygens and assemblies that ended (done or failed), the latest first: what
    happened while the user was away."""
    rows = conn.execute(
        "SELECT 'take', t.id, s.job_id, t.finished_at FROM take t JOIN segment s ON s.id = t.segment_id "
        "WHERE t.status IN ('done', 'failed') "
        "UNION ALL SELECT 'keygen', id, job_id, finished_at FROM keygen WHERE status IN ('done', 'failed') "
        "UNION ALL SELECT 'assembly', id, job_id, finished_at FROM assembly WHERE status IN ('done', 'failed') "
        "ORDER BY 4 DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for kind, rid, job_id, finished in rows:
        r = _get(conn, kind, rid)
        item = {"kind": kind, "id": rid, "job_id": job_id, "status": r["status"], "finished_at": finished}
        if kind == "take":
            item.update(tier=r["tier"], segment_idx=_get(conn, "segment", r["segment_id"])["idx"])
        elif kind == "keygen":
            item.update(tier="keyframe", position=r["position"])
        item.update(_task_card(conn, kind, r, job_id))
        out.append(item)
    return out
