"""Assembly: colour-match the retained takes onto the first one, then concatenate them.

Runs on the CPU in the worker (same queue as renders). Frames go through ffmpeg pipes as raw
RGB. Takes are brought to the first take's size and fps. Segment i+1 starts on the keyframe
segment i ends on, so its first frame is dropped to avoid a repeated frame at each join.
Colour matching uses one lookup table per segment, computed from all its frames: a per-frame
match would flicker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import images


class AssemblyError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def _tool(name: str) -> str:
    env = os.environ.get(f"ZOETROPE_{name.upper()}")
    if env:
        return env
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg  # available in Wan2GP's venv

            return imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass
    raise AssemblyError(f"{name} not found on PATH")


def probe(path: Path) -> dict:
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    s = json.loads(out.stdout)["streams"][0]
    return {
        "width": int(s["width"]), "height": int(s["height"]),
        "fps": float(Fraction(s["r_frame_rate"])), "frames": int(s.get("nb_read_frames") or 0),
    }


def read_frames(path: Path, size: tuple[int, int] | None = None, fps: float | None = None) -> np.ndarray:
    info = probe(path)
    w, h = size or (info["width"], info["height"])
    filters = []
    if fps and abs(fps - info["fps"]) > 1e-3:
        filters.append(f"fps={fps}")
    if (w, h) != (info["width"], info["height"]):
        filters.append(f"scale={w}:{h}:flags=lanczos")
    cmd = [_tool("ffmpeg"), "-v", "error", "-i", str(path)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(-1, h, w, 3)


def write_video(frames: np.ndarray, fps: float, path: Path) -> None:
    n, h, w, _ = frames.shape
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp" + path.suffix)
    cmd = [
        _tool("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "-",
        "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", str(tmp),
    ]
    proc = subprocess.run(cmd, input=np.ascontiguousarray(frames).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise AssemblyError(proc.stderr.decode(errors="replace")[-2000:])
    tmp.replace(path)


def assemble(
    take_files: list[Path],
    out_path: Path,
    *,
    color_match: bool = True,
    check_cancel: Callable[[], bool] = lambda: False,
    on_phase: Callable[[str], None] = lambda _phase: None,
) -> dict:
    """Concatenate the takes into out_path at the first take's size and fps. Returns its info."""
    if not take_files:
        raise AssemblyError("Nothing to assemble")
    first = probe(take_files[0])
    w, h = first["width"] - first["width"] % 2, first["height"] - first["height"] % 2
    fps = first["fps"]
    reference_hist = None
    parts = []
    for i, f in enumerate(take_files):
        if check_cancel():
            raise Cancelled
        on_phase(f"reading {i + 1}/{len(take_files)}")
        frames = read_frames(f, (w, h), fps)
        if i > 0:
            frames = frames[1:]
        if color_match:
            hist = images.histograms(frames)
            if reference_hist is None:
                reference_hist = hist
            else:
                frames = images.apply_luts(frames, images.matching_luts(hist, reference_hist))
        parts.append(frames)
    if check_cancel():
        raise Cancelled
    on_phase("encoding")
    video = np.concatenate(parts)
    write_video(video, fps, out_path)
    return {"width": w, "height": h, "fps": fps, "frames": int(video.shape[0])}
