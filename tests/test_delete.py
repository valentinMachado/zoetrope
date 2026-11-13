"""Deleting jobs, takes, created images, assemblies and the last keyframe, files included (P19a);
the summaries the projects page and the queue panel read."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import png_bytes, portrait
from zoetrope import db, paths


@pytest.fixture
def client(outputs):
    from zoetrope.api import app

    return TestClient(app)


def _job(client, n=2):
    job = client.post("/api/jobs", json={"title": "t"}).json()
    for pos in range(n):
        r = client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": pos},
                        files={"file": (f"{pos}.png", png_bytes(portrait(offset=pos)), "image/png")})
        assert r.status_code == 201, r.text
    return client.get(f"/api/jobs/{job['id']}").json()


def _take(client, seg, status="done"):
    """A final take on the segment with an output video file, in the given status."""
    client.patch(f"/api/segments/{seg['id']}", json={"prompt": "she smiles"})
    t = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "final"}).json()
    out = paths.takes_dir(seg["job_id"]) / f"{t['id']}.mp4"
    out.write_bytes(b"x" * 1000)
    conn = db.connect()
    db.update(conn, "take", t["id"], status=status, output_file=str(out), finished_at=db.now())
    conn.close()
    return t


def _keygen(client, job, status="done"):
    g = client.post(f"/api/jobs/{job['id']}/keygens",
                    data={"position": 1, "instruction": "smile", "sources": json.dumps([{"keyframe": 0}])}).json()
    out = paths.keygens_dir(job["id"]) / f"{g['id']}.png"
    out.write_bytes(png_bytes(portrait(color=(10, 200, 10))))
    conn = db.connect()
    db.update(conn, "keygen", g["id"], status=status, output_file=str(out), finished_at=db.now())
    conn.close()
    return g


def test_delete_take_removes_its_files_and_unretains(client):
    job = _job(client)
    seg = job["segments"][0]
    t = _take(client, seg)
    client.put(f"/api/segments/{seg['id']}/retained", json={"take_id": t["id"]})
    files = list(paths.takes_dir(job["id"]).glob(f"{t['id']}*"))
    assert len(files) >= 3  # start, end, settings, video
    r = client.delete(f"/api/takes/{t['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["freed_bytes"] >= 1000
    assert not any(f.exists() for f in files)
    seg = client.get(f"/api/jobs/{job['id']}").json()["segments"][0]
    assert seg["takes"] == [] and seg["retained_take_id"] is None


def test_delete_take_refuses_live_and_assembling(client):
    job = _job(client)
    seg = job["segments"][0]
    t = _take(client, seg)
    queued = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "preview"}).json()
    assert client.delete(f"/api/takes/{queued['id']}").status_code == 400
    client.put(f"/api/segments/{seg['id']}/retained", json={"take_id": t["id"]})
    client.post(f"/api/takes/{queued['id']}/cancel")
    client.post(f"/api/jobs/{job['id']}/assemblies", json={})
    r = client.delete(f"/api/takes/{t['id']}")
    assert r.status_code == 400 and "assembl" in r.json()["detail"]


def test_delete_replayed_take_keeps_the_inputs_it_shares(client):
    job = _job(client)
    seg = job["segments"][0]
    t = _take(client, seg)
    replay = client.post(f"/api/takes/{t['id']}/replay").json()
    start = paths.takes_dir(job["id"]) / f"{t['id']}_start.png"
    assert replay["settings"]["image_start"] == str(start)
    client.delete(f"/api/takes/{t['id']}")
    assert start.exists()  # the replay still reads it
    assert not (paths.takes_dir(job["id"]) / f"{t['id']}.mp4").exists()


def test_delete_keygen_refuses_the_current_image(client):
    job = _job(client)
    g = _keygen(client, job)
    assert client.post(f"/api/keygens/{g['id']}/use").status_code == 200
    r = client.delete(f"/api/keygens/{g['id']}")
    assert r.status_code == 400
    other = _keygen(client, job)  # not used: an older draw in the history
    files = list(paths.keygens_dir(job["id"]).glob(f"{other['id']}[._]*"))
    assert files
    assert client.delete(f"/api/keygens/{other['id']}").status_code == 200
    assert not any(f.exists() for f in files)
    assert (paths.keygens_dir(job["id"]) / f"{g['id']}.png").exists()
    ids = [x["id"] for x in client.get(f"/api/jobs/{job['id']}").json()["keygens"]]
    assert ids == [g["id"]]


def test_delete_assembly(client):
    job = _job(client)
    seg = job["segments"][0]
    t = _take(client, seg)
    client.put(f"/api/segments/{seg['id']}/retained", json={"take_id": t["id"]})
    a = client.post(f"/api/jobs/{job['id']}/assemblies", json={}).json()
    assert client.delete(f"/api/assemblies/{a['id']}").status_code == 400  # queued
    out = paths.assembly_dir(job["id"]) / f"{a['id']}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"v" * 500)
    conn = db.connect()
    db.update(conn, "assembly", a["id"], status="done", file=str(out), finished_at=db.now())
    conn.close()
    assert client.delete(f"/api/assemblies/{a['id']}").json()["freed_bytes"] == 500
    assert not out.exists()


def test_removing_the_last_keyframe_removes_its_files(client):
    job = _job(client)
    k = job["keyframes"][1]
    files = [paths.keyframes_dir(job["id"]) / n for n in ("1.png", "1_orig.png")]
    assert all(f.exists() for f in files)
    assert client.delete(f"/api/keyframes/{k['id']}").status_code == 204
    assert not any(f.exists() for f in files)


def test_delete_job_refuses_live_tasks_then_removes_everything(client):
    job = _job(client)
    seg = job["segments"][0]
    _take(client, seg)
    queued = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "preview"}).json()
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 400
    client.post(f"/api/takes/{queued['id']}/cancel")
    r = client.delete(f"/api/jobs/{job['id']}")
    assert r.status_code == 200 and r.json()["freed_bytes"] > 1000
    assert not paths.job_dir(job["id"]).exists()
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    conn = db.connect()
    for table in ("keyframe", "segment", "take", "keygen", "assembly"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    conn.close()


def test_job_list_summary_and_order(client):
    old = _job(client)
    new = _job(client, n=1)
    t = _take(client, old["segments"][0])  # the old job becomes the most recently active
    client.put(f"/api/segments/{old['segments'][0]['id']}/retained", json={"take_id": t["id"]})
    client.post(f"/api/segments/{old['segments'][0]['id']}/takes", json={"tier": "preview"})
    jobs = client.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [old["id"], new["id"]]
    j = jobs[0]
    assert (j["keyframes"], j["segments"], j["retained"], j["takes"], j["takes_done"]) == (2, 1, 1, 2, 1)
    assert (j["running"], j["queued"]) == (0, 1)
    assert j["video_url"] is None and j["disk_bytes"] > 1000


def test_queue_gives_thumbnails_and_recent_tasks(client):
    job = _job(client)
    seg = job["segments"][0]
    done = _take(client, seg)
    failed = _take(client, seg, status="failed")
    queued = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "preview"}).json()
    q = client.get("/api/queue").json()
    item = q["items"][0]
    assert item["id"] == queued["id"] and item["job_title"] == "t"
    assert item["thumb_url"].endswith(f"takes/{queued['id']}_start.png")
    recent = q["recent"]
    assert [(r["id"], r["status"]) for r in recent] == [(failed["id"], "failed"), (done["id"], "done")]
    assert recent[0]["segment_idx"] == 0 and recent[0]["tier"] == "final"
