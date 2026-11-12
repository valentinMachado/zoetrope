import numpy as np
from PIL import Image

from conftest import portrait
from zoetrope import images


def test_flatten_removes_fringes():
    img = portrait(color=(250, 200, 180))
    a = np.asarray(img).copy()
    # a half-transparent black ring around the disc, as a premultiplied cut-out leaves,
    edge = (a[..., 3] == 0) & (np.roll(a[..., 3], 1, axis=1) == 255)
    a[edge] = (0, 0, 0, 128)
    # and a 2-pixel opaque streak of the old background stuck to the edge
    rows = np.nonzero(edge.any(axis=1))[0][:5]
    streak = np.zeros_like(edge)
    for r in rows:
        c = np.nonzero(edge[r])[0][0]
        streak[r, c : c + 2] = True
    a[streak] = (255, 0, 0, 255)
    img = Image.fromarray(a, "RGBA")
    raw = np.asarray(images.flatten(img, remove_fringes=False)).astype(int)
    clean = np.asarray(images.flatten(img, remove_fringes=True)).astype(int)
    assert raw[edge & ~streak].min() < 140  # dark ring without the fix
    assert clean[edge & ~streak].min() > 200  # gone with it
    assert raw[streak][:, 1].max() < 10  # red streak without the fix
    assert clean[streak][:, 1].min() > 180  # mostly background with it
    assert tuple(clean[0, 0]) == (255, 255, 255)


def test_flatten_opaque_is_plain_rgb():
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    assert images.flatten(img).getpixel((0, 0)) == (1, 2, 3)


def test_fit_to_covers_and_crops():
    img = Image.new("RGB", (200, 100))
    out = images.fit_to(img, (50, 50))
    assert out.size == (50, 50)


def test_match_colors_brings_histogram_deviation_to_near_zero():
    ref = images.flatten(portrait(color=(200, 120, 90)))
    other = images.flatten(portrait(color=(120, 160, 220), offset=6))
    before = images.deviation(other, ref)
    matched = images.match_colors(other, ref)
    after = images.deviation(matched, ref)
    assert before["color"] > 10
    assert after["color"] < 2
    assert images.deviation(ref, ref) == {"color": 0.0, "pixel": 0.0}


def test_prepare_fits_to_start_and_matches():
    start = images.flatten(portrait(size=(96, 80)))
    kf = portrait(size=(150, 100), color=(90, 90, 200))
    out = images.prepare(kf, images.PrepParams(fit=True, color_match=True), start)
    assert out.size == start.size
    assert images.deviation(out, start)["color"] < images.deviation(images.flatten(kf), start)["color"] / 5


def test_parse_color():
    assert images.parse_color("#FF8000") == (255, 128, 0)
