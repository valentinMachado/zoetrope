import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import png_bytes, portrait
from zoetrope import paths


@pytest.fixture
def client(outputs):
    from zoetrope.api import app

    return TestClient(app)


def _job_with_keyframes(client, n=2, end_mode="exact"):
    job = client.post("/api/jobs", json={"title": "t", "end_mode": end_mode}).json()
    for pos in range(n):
        if pos == 0:
            img = portrait(color=(200, 120, 90))
        else:
            img = portrait(size=(140, 100), color=(120, 160, 220), offset=5)
        r = client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": pos},
                        files={"file": (f"{pos}.png", png_bytes(img), "image/png")})
        assert r.status_code == 201, r.text
    return client.get(f"/api/jobs/{job['id']}").json()


def test_segments_follow_keyframes(client):
    job = _job_with_keyframes(client, n=1)
    assert [(s["kf_in"], s["kf_out"]) for s in job["segments"]] == [(0, None)]
    job = _job_with_keyframes(client, n=3, end_mode="guide")
    segs = [(s["kf_in"], s["kf_out"], s["end_mode"]) for s in job["segments"]]
    assert segs == [(0, 1, "exact"), (1, 2, "guide")]
    assert job["keyframes"][1]["deviation"]["before"]["color"] > 0
    assert job["keyframes"][0]["url"].startswith("/files/")


def test_keyframe_position_must_be_contiguous(client):
    job = client.post("/api/jobs", json={}).json()
    r = client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": 2},
                    files={"file": ("a.png", png_bytes(portrait()), "image/png")})
    assert r.status_code == 400


def test_match_brings_keyframe_to_start_size_and_colours(client):
    job = _job_with_keyframes(client)
    kf = job["keyframes"][1]
    r = client.post(f"/api/keyframes/{kf['id']}/match", json={})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["match_applied"] == 1
    assert m["deviation"]["after"]["color"] < m["deviation"]["before"]["color"]
    start = Image.open(paths.keyframes_dir(job["id"]) / "0.png")
    assert Image.open(paths.keyframes_dir(job["id"]) / "1.png").size == start.size
    undo = client.post(f"/api/keyframes/{kf['id']}/match", json={"enabled": False}).json()
    assert undo["match_applied"] == 0


def test_takes_preview_final_cancel_replay_retain(client):
    job = _job_with_keyframes(client)
    seg = job["segments"][0]
    no_prompt = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "preview"})
    assert no_prompt.status_code == 400
    client.patch(f"/api/segments/{seg['id']}", json={"prompt": "the man smiles", "duration_s": 2.5})

    prev = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "preview", "seed": 42}).json()
    assert prev["settings"]["image_prompt_type"] == "S"
    assert prev["settings"]["model_type"] == "ti2v_2_2_fastwan"
    assert prev["settings"]["video_length"] == 61 and prev["seed"] == 42

    final = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "final"}).json()
    s = final["settings"]
    assert s["image_prompt_type"] == "SE" and s["video_length"] == 41
    assert s["image_start"].endswith(f"{final['id']}_start.png")
    assert s["image_end"].endswith(f"{final['id']}_end.png")
    assert isinstance(final["seed"], int)

    q = client.get("/api/queue").json()
    assert [i["id"] for i in q["items"]] == [prev["id"], final["id"]]
    assert q["worker"]["alive"] is False
    assert client.get(f"/api/takes/{final['id']}").json()["remaining_s"] > 0

    assert client.post(f"/api/takes/{prev['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/takes/{prev['id']}/cancel").status_code == 400
    again = client.post(f"/api/takes/{final['id']}/replay").json()
    assert again["seed"] == final["seed"] and again["settings"] == final["settings"]

    assert client.put(f"/api/segments/{seg['id']}/retained", json={"take_id": final["id"]}).status_code == 400
    assert client.post(f"/api/jobs/{job['id']}/assemblies", json={}).status_code == 400


def test_guide_mode_and_5b_preset(client):
    job = _job_with_keyframes(client, end_mode="guide")
    seg = job["segments"][0]
    client.patch(f"/api/segments/{seg['id']}", json={"prompt": "smile"})
    t = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "final"}).json()
    assert t["settings"]["image_prompt_type"] == "S"
    client.patch(f"/api/jobs/{job['id']}", json={"end_mode": "exact", "preset_final": "final-5b"})
    r = client.post(f"/api/segments/{seg['id']}/takes", json={"tier": "final"})
    assert r.status_code == 400 and "end frame" in r.json()["detail"]


def test_presets_endpoint(client):
    names = [p["name"] for p in client.get("/api/presets").json()]
    assert "final-14b" in names


def test_start_background_is_followed_by_the_other_keyframes(client):
    job = _job_with_keyframes(client, n=3)
    kfs = job["keyframes"]
    client.post(f"/api/keyframes/{kfs[1]['id']}/match", json={})
    r = client.post(f"/api/keyframes/{kfs[0]['id']}/match", json={"background": "#f5f5f5"})
    assert r.status_code == 200, r.text
    assert r.json()["match_params"]["background"] == [245, 245, 245]
    assert r.json()["version"] != kfs[0]["version"]
    d = paths.keyframes_dir(job["id"])
    assert Image.open(d / "0.png").getpixel((0, 0)) == (245, 245, 245)
    after = client.get(f"/api/jobs/{job['id']}").json()["keyframes"]
    for k in after[1:]:
        assert k["match_params"]["background"] == [245, 245, 245]
    assert after[1]["match_params"]["color_match"] is True  # its own match settings are kept
    assert Image.open(d / "2.png").getpixel((0, 0)) == (245, 245, 245)  # unmatched: flattened on it
    # A keyframe uploaded later is flattened on the start's background too.
    r = client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": 3},
                    files={"file": ("3.png", png_bytes(portrait()), "image/png")})
    assert r.json()["match_params"]["background"] == [245, 245, 245]


def test_front_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/presets").json()[0]["estimates"]


def test_keygen_routes(client):
    job = _job_with_keyframes(client, n=2)
    url = f"/api/jobs/{job['id']}/keygens"
    start = json.dumps([{"keyframe": 0}])
    assert client.post(url, data={"position": 3, "instruction": "smile", "sources": start}).status_code == 400
    assert client.post(url, data={"position": 1, "instruction": "  "}).status_code == 400  # no prompt, no source
    assert client.post(url, data={"position": 1, "sources": json.dumps([{"keyframe": 5}])}).status_code == 400
    assert client.post(url, data={"position": 1, "sources": "[{\"upload\": 0}]"}).status_code == 400  # no file
    assert client.post(url, data={"position": 1, "sources": json.dumps([{"keyframe": 0}] * 4)}).status_code == 400
    g = client.post(url, data={"position": 1, "instruction": "Make him smile", "seed": 5, "sources": start}).json()
    assert g["status"] == "queued" and g["seed"] == 5
    assert g["settings"]["image_refs"][0].replace("\\", "/").endswith(f"gen/{g['id']}_src0.png")
    assert g["sources"][0]["kind"] == "keyframe" and g["sources"][0]["url"].endswith(f"{g['id']}_src0.png")
    q = client.get("/api/queue").json()["items"]
    assert q[0]["kind"] == "keygen" and q[0]["position"] == 1
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "queued"
    assert client.post(f"/api/keygens/{g['id']}/use").status_code == 400  # not finished
    assert client.post(f"/api/keygens/{g['id']}/cancel").json()["status"] == "cancelled"
    again = client.post(f"/api/keygens/{g['id']}/again").json()
    assert again["id"] != g["id"] and again["sources"][0]["kind"] == "keyframe"
    assert again["instruction"] == "Make him smile" and again["settings"]["resolution"] == g["settings"]["resolution"]

    # the start image itself, from an uploaded image (with transparency) plus the start as a second reference
    files = [("files", ("ref.png", png_bytes(portrait()), "image/png"))]
    g = client.post(url, data={"position": 0, "sources": json.dumps([{"upload": 0}, {"keyframe": 0}])},
                    files=files).json()
    assert [s["kind"] for s in g["sources"]] == ["upload", "keyframe"] and len(g["settings"]["image_refs"]) == 2
    assert Image.open(paths.keygens_dir(job["id"]) / f"{g['id']}_src0.png").mode == "RGB"


def test_text_only_keygen_on_an_empty_job_takes_the_chosen_format(client):
    job = client.post("/api/jobs", json={}).json()
    url = f"/api/jobs/{job['id']}/keygens"
    assert client.post(url, data={"position": 1, "instruction": "A fox"}).status_code == 400  # only 0 exists
    assert client.post(url, data={"position": 0, "instruction": "A fox", "aspect": "wide"}).status_code == 400
    g = client.post(url, data={"position": 0, "instruction": "A fox", "aspect": "landscape"}).json()
    w, h = (int(v) for v in g["settings"]["resolution"].split("x"))
    assert w > h and g["sources"] == [] and g["settings"]["video_prompt_type"] == ""


def test_old_base_gets_the_new_keyframe_column(outputs):
    import sqlite3

    from zoetrope import db

    old = sqlite3.connect(paths.db_path())
    old.execute("CREATE TABLE keyframe (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, position INTEGER NOT NULL, "
                "orig_file TEXT NOT NULL, file TEXT NOT NULL, match_applied INTEGER NOT NULL DEFAULT 0, "
                "match_params TEXT, deviation TEXT, created_at REAL NOT NULL, UNIQUE (job_id, position))")
    old.close()
    conn = db.connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(keyframe)").fetchall()}
    conn.close()
    assert "source_keygen_id" in cols
