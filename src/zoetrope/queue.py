"""Queue order and time estimates, shared by the worker (what to run next) and the API (what the
front shows).

Order: prompt reviews first (~15 s, the user is reading the prompt), FIFO; then previews and
keyframe generations (keygens, ~40 s, the user waits for them), FIFO among them; then assemblies
(short, the user is waiting for them);
then final renders grouped by loaded model (model type + memory profile, since changing either
reloads the model), the group already loaded first, then the others by their oldest take, FIFO
inside a group.

Time estimates use, per preset, the median phase durations of its last finished takes, and the
preset's measured `estimates` until such takes exist.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any

from . import db, presets

MODEL_TASKS = ("take", "keygen")  # tasks that run a Wan2GP model; their tables share the progress columns
PHASES = ("loading_model", "encoding_text", "inference", "decoding")
ASSEMBLY_ESTIMATE_S = 30.0
RIFE_X2_ESTIMATE_S = 30.0  # P5: 21 s for 41 frames, model load included
REVIEW_ESTIMATE_S = 15.0  # P12: Qwen3 4B, 10-15 s load included
OTHER_ESTIMATES = {"assembly": ASSEMBLY_ESTIMATE_S, "review": REVIEW_ESTIMATE_S}


def other_estimate(task: Task) -> float:
    """Seconds for a task that runs no Wan2GP model: an assembly or a review."""
    extra = RIFE_X2_ESTIMATE_S if task.kind == "assembly" and task.row["fps_mode"] == "x2" else 0
    return OTHER_ESTIMATES[task.kind] + extra


@dataclass(frozen=True)
class Task:
    kind: str  # "take", "keygen", "assembly" or "review"
    id: int
    row: dict[str, Any]

    @property
    def group(self) -> tuple[str, float] | None:
        if self.kind not in MODEL_TASKS:
            return None
        return (self.row["model_type"], float(self.row["memory_profile"]))


def pending(conn: sqlite3.Connection, loaded: tuple[str, float] | None) -> list[Task]:
    """Queued tasks in the order the worker will run them."""
    takes = db.rows(conn.execute("SELECT * FROM take WHERE status = 'queued' ORDER BY id").fetchall())
    previews = [Task("take", t["id"], t) for t in takes if t["tier"] == "preview"]
    keygens = db.rows(conn.execute("SELECT * FROM keygen WHERE status = 'queued' ORDER BY id").fetchall())
    previews += [Task("keygen", k["id"], k) for k in keygens]
    previews.sort(key=lambda t: (t.row["created_at"], t.kind, t.id))
    finals = [Task("take", t["id"], t) for t in takes if t["tier"] == "final"]
    assemblies = [
        Task("assembly", a["id"], a)
        for a in db.rows(conn.execute("SELECT * FROM assembly WHERE status = 'queued' ORDER BY id").fetchall())
    ]
    groups: dict[tuple[str, float], list[Task]] = {}
    for t in finals:
        groups.setdefault(t.group, []).append(t)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] != loaded, kv[1][0].id))
    reviews = [Task("review", r["id"], r) for r in db.rows(conn.execute(
        "SELECT * FROM review WHERE status = 'queued' ORDER BY id").fetchall())]
    return reviews + previews + assemblies + [t for _, ts in ordered for t in ts]


def next_task(conn: sqlite3.Connection, loaded: tuple[str, float] | None) -> Task | None:
    tasks = pending(conn, loaded)
    return tasks[0] if tasks else None


# --- estimates --------------------------------------------------------------------------------


def phase_estimates(conn: sqlite3.Connection, preset_name: str, table: str = "take") -> dict[str, float]:
    """Seconds per phase for a preset: loading_model, encoding_text, inference_step, decoding.
    `table` is where its finished tasks live: take, or keygen for a keyframe preset."""
    try:
        est = dict(presets.load(preset_name).estimates)
    except presets.PresetError:
        est = {}
    done = conn.execute(
        f"SELECT durations FROM {table} WHERE preset = ? AND status = 'done' AND durations IS NOT NULL "
        "ORDER BY id DESC LIMIT 5",
        (preset_name,),
    ).fetchall()
    samples: dict[str, list[float]] = {}
    for (text,) in done:
        d = db.loads(text, {})
        ph = d.get("phases", {})
        if ph.get("loading_model", 0) > 5:  # a take that found the model loaded tells nothing
            samples.setdefault("loading_model", []).append(ph["loading_model"])
        for key in ("encoding_text", "decoding"):
            if key in ph:
                samples.setdefault(key, []).append(ph[key])
        if ph.get("inference") and d.get("steps"):
            samples.setdefault("inference_step", []).append(ph["inference"] / d["steps"])
    for key, values in samples.items():
        est[key] = statistics.median(values)
    return est


def _steps(take: dict[str, Any]) -> int:
    return int(db.loads(take["settings"], {}).get("num_inference_steps", 1))


def take_total_estimate(est: dict[str, float], take: dict[str, Any], needs_load: bool) -> float:
    total = est.get("encoding_text", 0) + est.get("inference_step", 0) * _steps(take) + est.get("decoding", 0)
    return total + (est.get("loading_model", 0) if needs_load else 0)


def take_remaining(est: dict[str, float], take: dict[str, Any], now: float, needs_load: bool = True) -> float:
    """Seconds left for a running take, from its current phase. Before Wan2GP's first event
    (phase None or 'starting') the whole take is ahead, with a model load if `needs_load`."""
    phase = take.get("phase")
    if phase in (None, "starting"):
        return take_total_estimate(est, take, needs_load)
    steps = _steps(take)
    durations = {
        "loading_model": est.get("loading_model", 0),
        "encoding_text": est.get("encoding_text", 0),
        "inference": est.get("inference_step", 0) * steps,
        "decoding": est.get("decoding", 0),
    }
    if phase not in PHASES:
        phase = "decoding"  # saving, downloading output: the end of the take
    i = PHASES.index(phase)
    elapsed = now - (take.get("phase_started_at") or now)
    current = max(0.0, durations[phase] - elapsed)
    return current + sum(durations[p] for p in PHASES[i + 1 :])


def snapshot(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    """Running task plus pending tasks, each with an estimated start and remaining time."""
    w = db.row(conn.execute("SELECT * FROM worker WHERE id = 1").fetchone()) or {}
    loaded = (w["loaded_model"], float(w["loaded_profile"])) if w.get("loaded_model") else None
    cache: dict[tuple[str, str], dict[str, float]] = {}

    def est_for(kind: str, name: str) -> dict[str, float]:
        if (kind, name) not in cache:
            cache[(kind, name)] = phase_estimates(conn, name, kind)
        return cache[(kind, name)]

    items = []
    clock = 0.0
    running = [(kind, t) for kind in MODEL_TASKS
               for t in db.rows(conn.execute(f"SELECT * FROM {kind} WHERE status = 'running'").fetchall())]
    running_other = [Task(kind, r["id"], r) for kind in ("assembly", "review")
                     for r in db.rows(conn.execute(f"SELECT * FROM {kind} WHERE status = 'running'").fetchall())]
    for kind, t in running:
        group = (t["model_type"], float(t["memory_profile"]))
        left = take_remaining(est_for(kind, t["preset"]), t, now, needs_load=group != loaded)
        items.append({"kind": kind, "id": t["id"], "status": "running", "starts_in_s": 0, "remaining_s": left})
        clock += left
        loaded = group
    for task in running_other:
        left = max(0.0, other_estimate(task) - (now - (task.row["started_at"] or now)))
        items.append({"kind": task.kind, "id": task.id, "status": "running", "starts_in_s": 0, "remaining_s": left})
        clock += left
    for task in pending(conn, loaded):
        if task.kind in MODEL_TASKS:
            needs_load = task.group != loaded
            dur = take_total_estimate(est_for(task.kind, task.row["preset"]), task.row, needs_load)
            loaded = task.group
        else:
            dur = other_estimate(task)
        items.append({"kind": task.kind, "id": task.id, "status": "queued", "starts_in_s": clock, "remaining_s": dur})
        clock += dur
    return {"items": items, "total_s": clock}
