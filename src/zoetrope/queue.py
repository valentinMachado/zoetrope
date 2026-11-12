"""Queue order and time estimates, shared by the worker (what to run next) and the API (what the
front shows).

Order: previews first, FIFO among them; then assemblies (short, the user is waiting for them);
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

PHASES = ("loading_model", "encoding_text", "inference", "decoding")
ASSEMBLY_ESTIMATE_S = 30.0
RIFE_X2_ESTIMATE_S = 30.0  # P5: 21 s for 41 frames, model load included


@dataclass(frozen=True)
class Task:
    kind: str  # "take" or "assembly"
    id: int
    row: dict[str, Any]

    @property
    def group(self) -> tuple[str, float] | None:
        if self.kind != "take":
            return None
        return (self.row["model_type"], float(self.row["memory_profile"]))


def pending(conn: sqlite3.Connection, loaded: tuple[str, float] | None) -> list[Task]:
    """Queued tasks in the order the worker will run them."""
    takes = db.rows(conn.execute("SELECT * FROM take WHERE status = 'queued' ORDER BY id").fetchall())
    previews = [Task("take", t["id"], t) for t in takes if t["tier"] == "preview"]
    finals = [Task("take", t["id"], t) for t in takes if t["tier"] == "final"]
    assemblies = [
        Task("assembly", a["id"], a)
        for a in db.rows(conn.execute("SELECT * FROM assembly WHERE status = 'queued' ORDER BY id").fetchall())
    ]
    groups: dict[tuple[str, float], list[Task]] = {}
    for t in finals:
        groups.setdefault(t.group, []).append(t)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] != loaded, kv[1][0].id))
    return previews + assemblies + [t for _, ts in ordered for t in ts]


def next_task(conn: sqlite3.Connection, loaded: tuple[str, float] | None) -> Task | None:
    tasks = pending(conn, loaded)
    return tasks[0] if tasks else None


# --- estimates --------------------------------------------------------------------------------


def phase_estimates(conn: sqlite3.Connection, preset_name: str) -> dict[str, float]:
    """Seconds per phase for a preset: loading_model, encoding_text, inference_step, decoding."""
    try:
        est = dict(presets.load(preset_name).estimates)
    except presets.PresetError:
        est = {}
    done = conn.execute(
        "SELECT durations FROM take WHERE preset = ? AND status = 'done' AND durations IS NOT NULL "
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
    cache: dict[str, dict[str, float]] = {}

    def est_for(name: str) -> dict[str, float]:
        if name not in cache:
            cache[name] = phase_estimates(conn, name)
        return cache[name]

    items = []
    clock = 0.0
    running = db.rows(conn.execute("SELECT * FROM take WHERE status = 'running'").fetchall())
    running_asm = db.rows(conn.execute("SELECT * FROM assembly WHERE status = 'running'").fetchall())
    for t in running:
        group = (t["model_type"], float(t["memory_profile"]))
        left = take_remaining(est_for(t["preset"]), t, now, needs_load=group != loaded)
        items.append({"kind": "take", "id": t["id"], "status": "running", "starts_in_s": 0, "remaining_s": left})
        clock += left
        loaded = (t["model_type"], float(t["memory_profile"]))
    for a in running_asm:
        left = ASSEMBLY_ESTIMATE_S + (RIFE_X2_ESTIMATE_S if a["fps_mode"] == "x2" else 0)
        items.append({"kind": "assembly", "id": a["id"], "status": "running", "starts_in_s": 0, "remaining_s": left})
        clock += left
    for task in pending(conn, loaded):
        if task.kind == "take":
            needs_load = task.group != loaded
            dur = take_total_estimate(est_for(task.row["preset"]), task.row, needs_load)
            loaded = task.group
        else:
            dur = ASSEMBLY_ESTIMATE_S + (RIFE_X2_ESTIMATE_S if task.row["fps_mode"] == "x2" else 0)
        items.append({"kind": task.kind, "id": task.id, "status": "queued", "starts_in_s": clock, "remaining_s": dur})
        clock += dur
    return {"items": items, "total_s": clock}
