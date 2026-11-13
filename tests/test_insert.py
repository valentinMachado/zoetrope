"""Inserting, copying and removing keyframes in the middle, and the gesture cut (P15)."""

import pytest
from fastapi.testclient import TestClient

from conftest import png_bytes, portrait
from zoetrope import db, paths


@pytest.fixture
def client(outputs):
    from zoetrope.api import app

    return TestClient(app)


def _job(client, n):
    job = client.post("/api/jobs", json={"title": "t"}).json()
    for pos in range(n):
        img = portrait(color=(200, 120 + 30 * pos, 90), offset=pos)
        r = client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": pos},
                        files={"file": (f"{pos}.png", png_bytes(img), "image/png")})
        assert r.status_code == 201, r.text
    return client.get(f"/api/jobs/{job['id']}").json()


def _get(client, job):
    return client.get(f"/api/jobs/{job['id']}").json()


def _done_take(client, seg, retain=True):
    """A finished final take on the segment, retained."""
    client.patch(f"/api/segments/{seg['id']}", json={"prompt": f"prompt {seg['idx']}"})
    t = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "final"}).json()
    conn = db.connect()
    db.update(conn, "take", t["id"], status="done", output_file=str(paths.takes_dir(seg["job_id"]) / "x.mp4"))
    conn.close()
    if retain:
        assert client.put(f"/api/segments/{seg['id']}/retained", json={"take_id": t["id"]}).status_code == 200
    return t


def _pixels(job, position):
    k = next(k for k in job["keyframes"] if k["position"] == position)
    return (paths.keyframes_dir(job["id"]) / f"{position}.png").read_bytes(), k


def test_insert_upload_in_the_middle_cuts_the_segment(client):
    job = _job(client, 3)
    seg0, seg1 = job["segments"]
    t = _done_take(client, seg0)
    before = {k["id"]: (paths.keyframes_dir(job["id"]) / f"{k['position']}.png").read_bytes() for k in job["keyframes"]}
    r = client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": 1, "insert": "true"},
                    files={"file": ("n.png", png_bytes(portrait(color=(10, 200, 10))), "image/png")})
    assert r.status_code == 201, r.text
    job = _get(client, job)
    assert [k["position"] for k in job["keyframes"]] == [0, 1, 2, 3]
    # the old keyframes moved one further, files included
    moved = {k["id"]: k["position"] for k in job["keyframes"]}
    for kid, data in before.items():
        assert (paths.keyframes_dir(job["id"]) / f"{moved[kid]}.png").read_bytes() == data
    segs = {s["id"]: s for s in job["segments"]}
    assert [(s["kf_in"], s["kf_out"]) for s in job["segments"]] == [(0, 1), (1, 2), (2, 3)]
    # the cut segment keeps its end (and its takes and prompt) but not its retained take
    assert segs[seg0["id"]]["idx"] == 1 and segs[seg0["id"]]["prompt"] == "prompt 0"
    assert [x["id"] for x in segs[seg0["id"]]["takes"]] == [t["id"]] and segs[seg0["id"]]["retained_take_id"] is None
    assert segs[seg1["id"]]["idx"] == 2
    assert job["segments"][0]["prompt"] == "" and job["segments"][0]["takes"] == []


def test_a_copy_keeps_the_cut_segment_retained_take_and_removal_undoes_it(client):
    job = _job(client, 2)
    seg = job["segments"][0]
    t = _done_take(client, seg)
    r = client.post(f"/api/keyframes/{job['keyframes'][0]['id']}/copy", json={})
    assert r.status_code == 201, r.text
    job = _get(client, job)
    assert _pixels(job, 1)[0] == _pixels(job, 0)[0]
    kept = next(s for s in job["segments"] if s["id"] == seg["id"])
    assert (kept["kf_in"], kept["kf_out"], kept["retained_take_id"]) == (1, 2, t["id"])
    # removing the copy merges the segments back: the original segment, with its retained take
    copy = next(k for k in job["keyframes"] if k["position"] == 1)
    assert client.delete(f"/api/keyframes/{copy['id']}").status_code == 204
    job = _get(client, job)
    assert [(s["id"], s["kf_in"], s["kf_out"], s["retained_take_id"]) for s in job["segments"]] == \
        [(seg["id"], 0, 1, t["id"])]
    assert [k["position"] for k in job["keyframes"]] == [0, 1]


def test_removal_in_the_middle_is_refused_when_the_arriving_segment_has_takes(client):
    job = _job(client, 3)
    _done_take(client, job["segments"][0], retain=False)
    mid = job["keyframes"][1]
    r = client.delete(f"/api/keyframes/{mid['id']}")
    assert r.status_code == 400 and "takes" in r.json()["detail"]
    assert client.delete(f"/api/keyframes/{job['keyframes'][0]['id']}").status_code == 400  # the start
    removable = _job(client, 3)
    _done_take(client, removable["segments"][1])  # the leaving segment keeps its takes
    assert client.delete(f"/api/keyframes/{removable['keyframes'][1]['id']}").status_code == 204
    after = _get(client, removable)
    assert len(after["segments"]) == 1 and after["segments"][0]["id"] == removable["segments"][1]["id"]
    assert after["segments"][0]["retained_take_id"] is None  # its start image changed


def test_insert_position_bounds(client):
    job = _job(client, 2)
    url = f"/api/jobs/{job['id']}/keyframes"
    f = {"file": ("n.png", png_bytes(portrait()), "image/png")}
    assert client.post(url, data={"position": 0, "insert": "true"}, files=f).status_code == 400
    assert client.post(url, data={"position": 3, "insert": "true"}, files=f).status_code == 400
    assert client.post(url, data={"position": 2, "insert": "true"}, files=f).status_code == 201  # appends


def test_keygens_follow_their_keyframe(client):
    job = _job(client, 3)
    g = client.post(f"/api/jobs/{job['id']}/keygens", data={"position": 2, "instruction": "Make her smile"}).json()
    client.post(f"/api/keyframes/{job['keyframes'][0]['id']}/copy", json={})
    assert client.get(f"/api/jobs/{job['id']}").json()["keygens"][0]["position"] == 3
    # a finished keygen whose image is removed is detached
    conn = db.connect()
    db.update(conn, "keygen", g["id"], status="done", output_file=str(paths.keyframes_dir(job["id"]) / "1.png"))
    conn.close()
    job = _get(client, job)
    k3 = next(k for k in job["keyframes"] if k["position"] == 3)
    copy = next(k for k in job["keyframes"] if k["position"] == 1)
    assert client.delete(f"/api/keyframes/{copy['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").json()["keygens"][0]["position"] == 2  # moved back
    assert client.post(f"/api/keyframes/{k3['id']}/copy", json={}).status_code == 201  # now in the middle
    assert client.delete(f"/api/keyframes/{k3['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").json()["keygens"][0]["position"] == -1
    assert client.post(f"/api/keygens/{g['id']}/use").status_code == 400


def test_a_queued_keygen_blocks_removing_its_image(client):
    job = _job(client, 3)
    client.post(f"/api/jobs/{job['id']}/keygens", data={"position": 1, "instruction": "Make her smile"})
    r = client.delete(f"/api/keyframes/{job['keyframes'][1]['id']}")
    assert r.status_code == 400 and "cancel" in r.json()["detail"]


def test_gesture_cut_from_the_end_image_with_return(client):
    # start -> smile -> other: the wink goes after the smile and comes back to it
    job = _job(client, 3)
    seg0, seg1 = job["segments"]
    t0 = _done_take(client, seg0)
    t1 = _done_take(client, seg1)
    smile = _pixels(job, 1)[0]
    r = client.post(f"/api/segments/{seg0['id']}/gesture", json={
        "instruction": "Make her wink", "base": "end", "back": True,
        "prompt": "She slowly closes her right eye", "back_prompt": "She opens her right eye again"})
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["position"] == 2 and g["sources"][0]["position"] == 1
    job = _get(client, job)
    assert [k["position"] for k in job["keyframes"]] == [0, 1, 2, 3, 4]
    assert _pixels(job, 2)[0] == smile and _pixels(job, 3)[0] == smile  # gesture slot and return copy
    segs = [(s["id"], s["kf_in"], s["kf_out"], s["prompt"], s["retained_take_id"]) for s in job["segments"]]
    assert segs[0] == (seg0["id"], 0, 1, "prompt 0", t0["id"])  # untouched
    assert segs[1][1:] == (1, 2, "She slowly closes her right eye", None)
    assert segs[2][1:] == (2, 3, "She opens her right eye again", None)
    assert segs[3] == (seg1["id"], 3, 4, "prompt 1", t1["id"])  # starts from an identical copy: still valid


def test_gesture_cut_from_the_start_without_return_and_on_a_lone_image(client):
    job = _job(client, 2)
    seg = job["segments"][0]
    _done_take(client, seg)
    r = client.post(f"/api/segments/{seg['id']}/gesture", json={"instruction": "Make her wink", "base": "start",
                                                                 "back": False})
    assert r.status_code == 201, r.text
    job = _get(client, job)
    segs = [(s["id"], s["kf_in"], s["kf_out"], s["retained_take_id"]) for s in job["segments"]]
    assert segs[1] == (seg["id"], 1, 2, None)  # now starts from the gesture: un-retained
    lone = _job(client, 1)
    seg = lone["segments"][0]
    assert client.post(f"/api/segments/{seg['id']}/gesture",
                       json={"instruction": "x", "base": "end"}).status_code == 400  # no end image
    assert client.post(f"/api/segments/{seg['id']}/gesture", json={"instruction": " "}).status_code == 400
    r = client.post(f"/api/segments/{seg['id']}/gesture", json={"instruction": "Make her wink", "base": "start"})
    assert r.status_code == 201, r.text
    lone = _get(client, lone)
    assert [(s["kf_in"], s["kf_out"]) for s in lone["segments"]] == [(0, 1), (1, 2)]
