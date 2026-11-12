import json

from zoetrope import db, queue


def _take(conn, tier, model, profile=4, status="queued"):
    conn.execute("INSERT INTO job (id, created_at, preset_final) VALUES (1, 0, 'final-14b') ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO segment (id, job_id, idx, kf_in) VALUES (1, 1, 0, 0) ON CONFLICT DO NOTHING")
    preset = "preview" if tier == "preview" else "final-14b"
    cur = conn.execute(
        "INSERT INTO take (segment_id, tier, preset, model_type, memory_profile, settings, seed, fps, status, "
        "created_at) VALUES (1, ?, ?, ?, ?, ?, 1, 16, ?, 0)",
        (tier, preset, model, profile, json.dumps({"num_inference_steps": 4}), status),
    )
    return cur.lastrowid


def test_previews_first_then_assemblies_then_finals_grouped(conn):
    f1 = _take(conn, "final", "A")
    f2 = _take(conn, "final", "B")
    p1 = _take(conn, "preview", "P", 1)
    f3 = _take(conn, "final", "A")
    p2 = _take(conn, "preview", "P", 1)
    conn.execute("INSERT INTO assembly (id, job_id, take_ids, created_at) VALUES (9, 1, '[]', 0)")
    order = [(t.kind, t.id) for t in queue.pending(conn, loaded=None)]
    assert order == [("take", p1), ("take", p2), ("assembly", 9), ("take", f1), ("take", f3), ("take", f2)]
    # the group already loaded goes first
    order = [t.id for t in queue.pending(conn, loaded=("B", 4.0)) if t.kind == "take"]
    assert order == [p1, p2, f2, f1, f3]


def test_profile_is_part_of_the_group(conn):
    a4 = _take(conn, "final", "A", 4)
    a5 = _take(conn, "final", "A", 5)
    assert [t.id for t in queue.pending(conn, loaded=("A", 5.0))] == [a5, a4]


def test_snapshot_estimates_accumulate(conn):
    _take(conn, "final", "i2v_2_2_Enhanced_Lightning_v2", 5)
    _take(conn, "final", "i2v_2_2_Enhanced_Lightning_v2", 5)
    snap = queue.snapshot(conn, now=1000.0)
    a, b = snap["items"]
    assert a["starts_in_s"] == 0 and b["starts_in_s"] == a["remaining_s"]
    assert b["remaining_s"] < a["remaining_s"]  # second one finds the model loaded


def test_estimates_learn_from_finished_takes(conn):
    t = _take(conn, "final", "A", status="done")
    db.update(conn, "take", t, durations=json.dumps(
        {"phases": {"loading_model": 100, "encoding_text": 10, "inference": 400, "decoding": 20}, "steps": 4}))
    est = queue.phase_estimates(conn, "final-14b")
    assert est["inference_step"] == 100 and est["loading_model"] == 100 and est["decoding"] == 20


def test_running_take_before_first_event_counts_the_whole_take(conn):
    t = _take(conn, "final", "i2v_2_2_Enhanced_Lightning_v2", 5, status="running")
    db.update(conn, "take", t, phase="starting")
    est = queue.phase_estimates(conn, "final-14b")
    left = queue.snapshot(conn, now=1000.0)["items"][0]["remaining_s"]
    assert left == queue.take_total_estimate(est, db.row(conn.execute("SELECT * FROM take").fetchone()), True)
    assert left > 1000
