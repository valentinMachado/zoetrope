import os
import tempfile

import numpy as np
import pytest
from PIL import Image

# Before any zoetrope import: keep tests away from the real outputs/.
os.environ.setdefault("ZOETROPE_OUTPUTS", tempfile.mkdtemp(prefix="zoetrope-tests-"))


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOETROPE_OUTPUTS", str(tmp_path))
    return tmp_path


@pytest.fixture
def conn(outputs):
    from zoetrope import db

    c = db.connect()
    yield c
    c.close()


def portrait(size=(96, 80), color=(200, 120, 90), alpha=True, offset=0):
    """A coloured disc on a transparent (or grey) background."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    inside = ((xx - w / 2 - offset) ** 2 + (yy - h / 2) ** 2) < (min(w, h) / 3) ** 2
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[inside, :3] = color
    rgba[inside, 3] = 255
    if not alpha:
        rgba[~inside, :3] = 128
        rgba[..., 3] = 255
    return Image.fromarray(rgba, "RGBA")


def png_bytes(img):
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
