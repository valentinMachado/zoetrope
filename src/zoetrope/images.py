"""Image preparation: alpha flattening, fringe removal, framing, colour matching, deviation.

The keyframe technical match (raccord technique) and the colour match of segments before
concatenation share the histogram code here. It fixes colour, format and background, not
identity (a model-based identity match is a later phase).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

WHITE = (255, 255, 255)


@dataclass
class PrepParams:
    """How a keyframe is prepared. Upload uses the defaults; the technical match turns on fit and
    color_match against the start image (keyframe 0)."""

    background: tuple[int, int, int] = WHITE
    remove_fringes: bool = True
    fit: bool = False
    color_match: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def parse_color(text: str) -> tuple[int, int, int]:
    text = text.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"Expected a colour like #ffffff, got {text!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


# --- alpha ------------------------------------------------------------------------------------


FRINGE_CHOKE_PX = 2


def flatten(img: Image.Image, background=WHITE, remove_fringes: bool = True) -> Image.Image:
    """RGB image with any alpha composited onto `background`.

    The edge of a cut-out carries leftovers of its old background: translucent halos, dark
    premultiplied pixels, thin opaque streaks. On a plain background they show as a fringe, which
    the video model wipes away at frame 1 (a visible jump, P3). With remove_fringes the matte is
    choked: eroded by FRINGE_CHOKE_PX pixels, then softened by one pixel, before compositing.
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
    else:
        return img.convert("RGB")
    matte = rgba.getchannel("A")
    if remove_fringes:
        matte = matte.filter(ImageFilter.MinFilter(2 * FRINGE_CHOKE_PX + 1)).filter(ImageFilter.GaussianBlur(1))
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.float32)
    alpha = np.asarray(matte, dtype=np.float32) / 255.0
    bg = np.array(background, dtype=np.float32)
    out = rgb * alpha[..., None] + bg * (1.0 - alpha[..., None])
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


def fit_to(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to cover `size` keeping the aspect ratio, then centre-crop to it."""
    if img.size == size:
        return img
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    w, h = max(tw, round(img.width * scale)), max(th, round(img.height * scale))
    img = img.resize((w, h), Image.LANCZOS)
    left, top = (w - tw) // 2, (h - th) // 2
    return img.crop((left, top, left + tw, top + th))


# --- histograms ---------------------------------------------------------------------------------


def histograms(pixels: np.ndarray) -> np.ndarray:
    """Per-channel 256-bin histograms (shape 3x256) of uint8 RGB pixels of any leading shape."""
    flat = pixels.reshape(-1, 3)
    return np.stack([np.bincount(flat[:, c], minlength=256) for c in range(3)]).astype(np.float64)


def matching_luts(source_hist: np.ndarray, reference_hist: np.ndarray) -> np.ndarray:
    """Per-channel lookup tables (3x256 uint8) that map the source histograms onto the reference."""
    # Quantiles of the levels actually present only (as scikit-image does): flat stretches of a
    # cumulative histogram would otherwise send whole ranges to the end of the stretch.
    luts = np.empty((3, 256), dtype=np.uint8)
    levels = np.arange(256, dtype=np.float64)
    for c in range(3):
        sv = np.nonzero(source_hist[c])[0]
        rv = np.nonzero(reference_hist[c])[0]
        sq = np.cumsum(source_hist[c][sv]) / source_hist[c].sum()
        rq = np.cumsum(reference_hist[c][rv]) / reference_hist[c].sum()
        mapped = np.interp(sq, rq, rv.astype(np.float64))
        lut = np.interp(levels, sv.astype(np.float64), mapped) if len(sv) > 1 else np.full(256, mapped[0])
        luts[c] = np.clip(lut + 0.5, 0, 255).astype(np.uint8)
    return luts


def apply_luts(pixels: np.ndarray, luts: np.ndarray) -> np.ndarray:
    out = np.empty_like(pixels)
    for c in range(3):
        out[..., c] = luts[c][pixels[..., c]]
    return out


def match_colors(img: Image.Image, reference: Image.Image) -> Image.Image:
    src = np.asarray(img.convert("RGB"))
    ref = np.asarray(reference.convert("RGB"))
    luts = matching_luts(histograms(src), histograms(ref))
    return Image.fromarray(apply_luts(src, luts), "RGB")


def deviation(img: Image.Image, reference: Image.Image) -> dict[str, float]:
    """How far a keyframe is from the start image, in grey levels (0-255):

    - color: mean over channels of the distance between colour histograms (earth mover's
      distance); 0 when the colour distributions are the same, whatever the layout.
    - pixel: mean absolute difference pixel by pixel, both images at 128 px wide; mixes layout
      (pose, framing) and colour.
    """
    a = np.asarray(img.convert("RGB"))
    b = np.asarray(reference.convert("RGB"))
    ha, hb = histograms(a), histograms(b)
    ca = np.cumsum(ha, axis=1) / ha.sum(axis=1, keepdims=True)
    cb = np.cumsum(hb, axis=1) / hb.sum(axis=1, keepdims=True)
    color = float(np.abs(ca - cb).sum(axis=1).mean())
    w = 128
    h = max(1, round(reference.height * w / reference.width))
    sa = np.asarray(img.convert("RGB").resize((w, h), Image.BILINEAR), dtype=np.float32)
    sb = np.asarray(reference.convert("RGB").resize((w, h), Image.BILINEAR), dtype=np.float32)
    pixel = float(np.abs(sa - sb).mean())
    return {"color": round(color, 2), "pixel": round(pixel, 2)}


#: Below this share of changed cells (see `changed_share`), a created image is taken as a copy of
#: its source: Flux 2 Klein copies the source when it does not understand the instruction.
#: Measured (docs/benchmark.md, "Telling an unchanged edit"): 0 for four copies, 0.003 for a wink.
UNCHANGED_BELOW = 0.001


def changed_share(img: Image.Image, source: Image.Image, side: int = 64, level: int = 40) -> float:
    """Share of an edit that visibly changed from its source, 0-1: both images in grey at `side`
    px on the long side (box filter, which smooths the model's re-encoding noise), the global
    brightness shift removed (median difference), then the share of cells off by more than `level`
    grey levels. A copy scores 0; a wink, the smallest edit asked of Klein here, ~0.003."""
    k = side / max(source.size)
    size = (max(1, round(source.width * k)), max(1, round(source.height * k)))
    a = np.asarray(img.convert("L").resize(size, Image.BOX), dtype=np.float32)
    b = np.asarray(source.convert("L").resize(size, Image.BOX), dtype=np.float32)
    d = a - b
    d = np.abs(d - np.median(d))
    return float((d > level).mean())


# --- keyframe preparation -----------------------------------------------------------------------


def prepare(orig: Image.Image, params: PrepParams, start: Image.Image | None) -> Image.Image:
    """The prepared keyframe sent to the model. `start` is the prepared start image (keyframe 0),
    None when preparing keyframe 0 itself."""
    img = flatten(orig, params.background, params.remove_fringes)
    if start is not None and params.fit:
        img = fit_to(img, start.size)
    if start is not None and params.color_match:
        img = match_colors(img, start)
    return img


def save_png(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.png")
    img.save(tmp, "PNG")
    tmp.replace(path)
