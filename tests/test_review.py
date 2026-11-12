"""Prompt review (P12): instant rules, the AI's messages and reply, the review task end to end
with a fake reviewer."""

import pytest
from fastapi.testclient import TestClient

from conftest import png_bytes, portrait
from zoetrope import db, prompt_review, service
from zoetrope import queue as task_queue
from zoetrope.prompt_review import Context, check, parse_reply
from zoetrope.worker import Worker

JOB4 = ("le visage sourit puis fait un clin d'oeil avant de se transformer en une multitude de papillon "
        "qui partent dans tous les sens")
GOOD = ("A watercolor painting of a woman's face, loose wet brushstrokes, paint splashes on white paper. "
        "She slowly smiles, then winks. Static camera.")


def codes(text, ctx):
    return [f["code"] for f in check(text, ctx)]


# --- rules --------------------------------------------------------------------------------------


def test_the_job_4_prompt_misses_language_style_camera_and_has_too_many_actions():
    assert codes(JOB4, Context("motion", duration_s=5)) == ["french", "no_style", "no_camera", "too_many_actions"]
    found = check(JOB4, Context("motion", duration_s=5))[-1]
    assert (found["actions"], found["max"]) == (3, 2)


def test_a_good_motion_prompt_passes():
    assert codes(GOOD, Context("motion", duration_s=3)) == []


def test_short_negation_and_empty():
    assert codes("cat jumps", Context("motion", duration_s=3)) == ["short", "no_style", "no_camera"]
    assert "negation" in codes("A photo of a man, no camera movement, he does not blink", Context("motion"))
    assert codes("  ", Context("motion")) == ["empty"]


def test_an_image_edit_needs_no_style_and_may_be_empty():
    edit = Context("image", position=1, sources=("start",))
    assert codes("", edit) == []
    assert codes("Make her close her eyes and smile widely", edit) == []
    assert codes("elle ferme les yeux et tourne la tête", edit) == ["french"]
    # From text alone the style must be named.
    assert codes("An old fisherman with a grey beard, facing the viewer", Context("image", position=0)) == ["no_style"]


def test_french_detection():
    assert prompt_review.is_french("la femme sourit doucement")
    assert prompt_review.is_french("Elle sourit à la caméra")
    assert not prompt_review.is_french("The woman smiles at the camera, then winks")
    assert not prompt_review.is_french("A watercolor painting, static camera")


def test_actions_allowed_grow_past_five_seconds():
    text = "He smiles, then nods, then waves. Static camera, a photo."
    assert "too_many_actions" in codes(text, Context("motion", duration_s=4))
    assert "too_many_actions" not in codes(text, Context("motion", duration_s=8))


# --- the AI's messages and reply ----------------------------------------------------------------


def test_messages_carry_context_and_rule_findings():
    ctx = Context("motion", duration_s=5, end_image="exact", notes=('The start image was created from "x"',))
    msgs = prompt_review.build_messages(JOB4, ctx)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    user = msgs[-1]["content"]
    assert "Duration: 5 s." in user and "end image" in user and 'created from "x"' in user
    assert "not in English" in user and "3 actions in sequence for 5 s: at most 2" in user
    assert user.endswith(JOB4)
    image = prompt_review.build_messages("x", Context("image", position=0))
    assert "Flux 2 Klein" in image[0]["content"] and "text alone" in image[-1]["content"]


def test_parse_reply_either_order_without_colons():
    r = parse_reply("PROMPT:\nA man smiles,\n then winks .\nOTHER:\n- rien")
    assert r == {"issues": [], "suggestion": "A man smiles, then winks."}
    r = parse_reply("OTHER\n- Le geste est flou\nPROMPT\nA cat jumps. Static camera.")
    assert r == {"issues": ["Le geste est flou"], "suggestion": "A cat jumps. Static camera."}
    assert parse_reply("A cat jumps.") == {"issues": [], "suggestion": "A cat jumps."}


def test_parse_reply_drops_cjk_and_a_placeholder_next_to_a_named_style():
    r = parse_reply("PROMPT:\n[style] of a watercolor painting. She winks 画面.\nOTHER:\n- rien")
    assert r["suggestion"] == "A watercolor painting. She winks."
    assert parse_reply("PROMPT:\n[style] of a cat. Static camera.")["suggestion"] == "[style] of a cat. Static camera."


# --- service, API, worker -----------------------------------------------------------------------


class FakeReviewer:
    VRAM_NEEDED = 5.0

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def review(self, text, ctx, should_stop=lambda: False, on_phase=lambda p: None):
        self.calls.append((text, ctx))
        on_phase("inference")
        if self.fail:
            raise RuntimeError("CUDA out of memory")
        return {"issues": ["flou"], "suggestion": "A smile.", "raw": "PROMPT:\nA smile.",
                "durations": {"total": 1.0}}


class ClosingSession:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _job(conn):
    job = service.create_job(conn, "r")
    service.add_keyframe(conn, job["id"], 0, png_bytes(portrait()))
    service.add_keyframe(conn, job["id"], 1, png_bytes(portrait()))
    seg = service.job_detail(conn, job["id"])["segments"][0]
    return job, seg


def test_context_tells_how_the_images_were_made(conn):
    job, seg = _job(conn)
    g = service.create_keygen(conn, job["id"], 1, "Make her wink", sources=[0])
    db.update(conn, "keyframe", conn.execute("SELECT id FROM keyframe WHERE position = 1").fetchone()[0],
              source_keygen_id=g["id"])
    ctx = service.review_context(conn, job["id"], "motion", segment_id=seg["id"], duration_s=4)
    assert ctx.duration_s == 4 and ctx.end_image == "exact"
    assert ctx.notes == ('The end image was created from this prompt: "Make her wink"',)
    img = service.review_context(conn, job["id"], "image", position=2, sources=[1, "upload"])
    assert img.sources == ("keyframe", "upload")
    assert img.notes == ('Source image 1 was created from this prompt: "Make her wink"',)


def test_review_runs_first_in_the_queue_and_stores_the_reply(conn):
    job, seg = _job(conn)
    service.update_segment(conn, seg["id"], prompt="smile")
    service.create_take(conn, seg["id"], "preview")
    r = service.create_review(conn, job["id"], "motion", JOB4, segment_id=seg["id"])
    assert r["status"] == "queued" and "french" in [f["code"] for f in r["findings"]]
    assert task_queue.next_task(conn, None).kind == "review"
    snap = service.queue_state(conn)
    assert snap["items"][0]["kind"] == "review" and snap["items"][0]["segment_idx"] == 0

    reviewer = FakeReviewer()
    session = ClosingSession()
    w = Worker(conn, session_factory=lambda: session, reviewer_factory=lambda: reviewer, free_vram=lambda: 1.0)
    w.session, w.loaded = session, ("ti2v_2_2", 1.0)  # a video model is loaded and VRAM is short
    assert w.run_next()
    done = service.job_detail(conn, job["id"])["reviews"][0]
    assert done["status"] == "done" and done["result"]["suggestion"] == "A smile."
    assert done["durations"] == {"total": 1.0}
    assert reviewer.calls[0][1].duration_s == seg["duration_s"]
    assert session.closed == 1 and w.loaded is None


def test_review_keeps_the_model_when_vram_suffices_and_fails_cleanly(conn):
    job, seg = _job(conn)
    service.create_review(conn, job["id"], "image", "elle sourit", position=1, sources=[0])
    session = ClosingSession()
    w = Worker(conn, reviewer_factory=lambda: FakeReviewer(fail=True), free_vram=lambda: 10.0)
    w.session, w.loaded = session, ("flux2_klein", 1.0)
    w.run_next()
    r = service.job_detail(conn, job["id"])["reviews"][0]
    assert r["status"] == "failed" and "out of memory" in r["error"]
    assert session.closed == 0 and w.loaded == ("flux2_klein", 1.0)


def test_review_needs_text_and_a_segment_of_the_job(conn):
    job, seg = _job(conn)
    other, _ = _job(conn)
    with pytest.raises(service.Invalid):
        service.create_review(conn, job["id"], "motion", " ", segment_id=seg["id"])
    with pytest.raises(service.Invalid):
        service.create_review(conn, other["id"], "motion", "x", segment_id=seg["id"])
    with pytest.raises(service.Invalid):
        service.create_review(conn, job["id"], "sound", "x")


def test_api_check_review_and_cancel(outputs):
    from zoetrope.api import app

    client = TestClient(app)
    job = client.post("/api/jobs", json={"title": "t"}).json()
    client.post(f"/api/jobs/{job['id']}/keyframes", data={"position": 0},
                files={"file": ("0.png", png_bytes(portrait()), "image/png")})
    seg = client.get(f"/api/jobs/{job['id']}").json()["segments"][0]
    r = client.post(f"/api/jobs/{job['id']}/prompt-check",
                    json={"target": "motion", "text": JOB4, "segment_id": seg["id"], "duration_s": 5})
    assert [f["code"] for f in r.json()["findings"]] == ["french", "no_style", "no_camera", "too_many_actions"]
    r = client.post(f"/api/jobs/{job['id']}/prompt-check",
                    json={"target": "image", "text": "", "position": 1, "sources": [{"keyframe": 0}, {"upload": 0}]})
    assert r.json()["findings"] == []
    r = client.post(f"/api/jobs/{job['id']}/reviews", json={"target": "image", "text": "a cat", "position": 0})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert client.get("/api/queue").json()["items"][0]["kind"] == "review"
    assert client.post(f"/api/reviews/{rid}/cancel").json()["status"] == "cancelled"
