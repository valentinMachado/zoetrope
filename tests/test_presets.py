from pathlib import Path

import pytest

from zoetrope import presets


def test_frames_follow_4n_plus_1():
    assert presets.frames_for(2.5, 24) == 61
    assert presets.frames_for(2.5, 16) == 41
    assert presets.frames_for(0.1, 16) == 5
    for d in (0.5, 1, 3.3, 5):
        assert (presets.frames_for(d, 16) - 1) % 4 == 0


def test_shipped_presets_load():
    names = {p.name: p for p in presets.list_presets()}
    assert {"preview", "final-14b", "final-5b"} <= set(names)
    assert names["preview"].tier == "preview" and not names["preview"].end_frame
    assert names["final-14b"].end_frame and names["final-14b"].fps == 16
    assert presets.DEFAULT_FINAL == "final-14b"


def test_build_settings_start_and_end():
    p = presets.load("final-14b")
    s = presets.build_settings(p, prompt="smiles", image_start=Path("a.png"), image_end=Path("b.png"),
                               duration_s=2.5, seed=7)
    assert s["image_prompt_type"] == "SE" and s["image_end"] == "b.png"
    assert s["video_length"] == 41 and s["seed"] == 7
    assert s["overrides"].startswith("memory_profile=")
    assert s["model_type"] == "i2v_2_2_Enhanced_Lightning_v2"
    s = presets.build_settings(p, prompt="smiles", image_start=Path("a.png"), image_end=None, duration_s=2.5, seed=7)
    assert s["image_prompt_type"] == "S" and "image_end" not in s


def test_no_end_frame_on_5b_and_style_appended():
    p = presets.load("preview")
    with pytest.raises(presets.PresetError):
        presets.build_settings(p, prompt="x", image_start=Path("a"), image_end=Path("b"), duration_s=2, seed=1)
    p.style = "watercolour"
    s = presets.build_settings(p, prompt="a cat", image_start=Path("a"), image_end=None, duration_s=2, seed=1)
    assert s["prompt"] == "a cat, watercolour"


def test_profile_override_format():
    assert presets.profile_override(4) == "memory_profile=4"
    assert presets.profile_override(4.5) == "memory_profile=4.5"


def test_keygen_resolution_keeps_the_start_aspect_within_the_budget():
    assert presets.fit_resolution("480x512", (480, 510)) == "480x512"
    w, h = (int(v) for v in presets.fit_resolution("480x512", (544, 736)).split("x"))
    assert w % 16 == 0 and h % 16 == 0
    assert abs(w / h - 544 / 736) < 0.03 and abs(w * h - 480 * 512) / (480 * 512) < 0.06


def test_build_keygen_settings():
    p = presets.load(presets.KEYFRAME)
    assert p.tier == "keyframe" and p.model_type == "flux2_klein_4b"
    s = presets.build_keygen_settings(p, instruction="Make her smile.", image=Path("s.png"),
                                      image_size=(480, 510), seed=3)
    assert s["image_refs"] == ["s.png"] and s["video_prompt_type"] == "KI" and s["image_mode"] == 1
    assert s["prompt"].startswith("Make her smile. Keep the same person")
    assert s["seed"] == 3 and s["resolution"] == "480x512" and s["num_inference_steps"] == 4
    with pytest.raises(presets.PresetError):
        presets.build_keygen_settings(presets.load("preview"), instruction="x", image=Path("s"),
                                      image_size=(1, 1), seed=1)
