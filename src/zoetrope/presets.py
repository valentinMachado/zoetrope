"""Presets: templates of Wan2GP settings, one JSON file per preset in `presets/`.

A preset holds the brief's fields (style, LoRA + weight, resolution, steps, cfg) plus what the
queue needs: its tier (preview or final), whether the model takes an end frame, its native fps,
the memory profile, and measured phase durations used for the time-remaining estimate until real
takes of the preset exist. A take snapshots the settings resolved here: editing a preset later
does not change past takes.

A `keyframe` preset (tier "keyframe", Flux 2 Klein) generates a keyframe by editing the start image
with a text instruction (docs/benchmark.md, "Identity match of a keyframe"). Its `resolution` is a
pixel budget: the output takes the start image's aspect ratio within it. Its `prompt_suffix`
follows the user's instruction and asks the model to keep what must not change.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

PREVIEW = "preview"
DEFAULT_FINAL = "final-14b"
KEYFRAME = "keyframe-klein"
TIERS = ("preview", "final", "keyframe")


class PresetError(ValueError):
    pass


@dataclass
class Preset:
    name: str
    label: str
    tier: str
    end_frame: bool
    fps: int
    memory_profile: float
    settings: dict[str, Any]
    style: str = ""
    loras: list[dict[str, Any]] = field(default_factory=list)
    estimates: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    prompt_suffix: str = ""

    @property
    def model_type(self) -> str:
        return self.settings["model_type"]

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "tier": self.tier, "end_frame": self.end_frame,
            "fps": self.fps, "memory_profile": self.memory_profile, "model_type": self.model_type,
            "style": self.style, "loras": self.loras, "notes": self.notes, "estimates": self.estimates,
            "steps": self.settings.get("num_inference_steps"), "resolution": self.settings.get("resolution"),
        }


def load(name: str, directory: Path | None = None) -> Preset:
    directory = directory or paths.PRESETS_DIR
    file = directory / f"{name}.json"
    if not file.is_file():
        raise PresetError(f"Unknown preset: {name}")
    data = json.loads(file.read_text(encoding="utf-8"))
    preset = Preset(name=name, **data)
    if preset.tier not in TIERS:
        raise PresetError(f"Preset {name}: unknown tier {preset.tier!r}")
    return preset


def list_presets(directory: Path | None = None) -> list[Preset]:
    directory = directory or paths.PRESETS_DIR
    return [load(f.stem, directory) for f in sorted(directory.glob("*.json"))]


def frames_for(duration_s: float, fps: int) -> int:
    """Frame count for a duration: Wan models take 4n+1 frames."""
    n = max(1, round(duration_s * fps) - 1)
    return 4 * max(1, round(n / 4)) + 1


def profile_override(profile: float) -> str:
    value = int(profile) if float(profile).is_integer() else profile
    return f"memory_profile={value}"


def new_seed() -> int:
    return random.randint(0, 2**31 - 1)


def build_settings(
    preset: Preset,
    *,
    prompt: str,
    image_start: Path,
    image_end: Path | None,
    duration_s: float,
    seed: int,
) -> dict[str, Any]:
    """The complete settings dict sent to Wan2GP for one take, seed included."""
    if image_end is not None and not preset.end_frame:
        raise PresetError(f"Preset {preset.name} takes no end frame")
    full_prompt = prompt.strip()
    if preset.style:
        full_prompt = f"{full_prompt}, {preset.style}" if full_prompt else preset.style
    settings: dict[str, Any] = dict(preset.settings)
    settings.update(
        prompt=full_prompt,
        image_prompt_type="SE" if image_end is not None else "S",
        image_start=str(image_start),
        video_length=frames_for(duration_s, preset.fps),
        seed=int(seed),
        overrides=profile_override(preset.memory_profile),
    )
    if image_end is not None:
        settings["image_end"] = str(image_end)
    if preset.loras:
        files = [paths.LORAS_DIR / lora["file"] for lora in preset.loras]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise PresetError(f"Missing LoRA file(s): {', '.join(missing)}")
        settings["activated_loras"] = [str(f) for f in files]
        settings["loras_multipliers"] = " ".join(str(lora.get("weight", 1.0)) for lora in preset.loras)
    return settings


def fit_resolution(budget: str, size: tuple[int, int], block: int = 16) -> str:
    """A WxH resolution with the aspect ratio of `size` and about the pixel count of `budget`
    ("480x512"), both sides multiples of `block`."""
    bw, bh = (int(v) for v in budget.lower().split("x"))
    w, h = size
    scale = ((bw * bh) / (w * h)) ** 0.5
    return f"{max(block, round(w * scale / block) * block)}x{max(block, round(h * scale / block) * block)}"


def build_keygen_settings(
    preset: Preset,
    *,
    instruction: str,
    image: Path,
    image_size: tuple[int, int],
    seed: int,
) -> dict[str, Any]:
    """Settings sent to Wan2GP to edit the start image `image` with a text instruction."""
    if preset.tier != "keyframe":
        raise PresetError(f"Preset {preset.name} is not a keyframe preset")
    prompt = instruction.strip()
    if preset.prompt_suffix:
        sep = " " if prompt.endswith((".", "!", "?")) else ". "
        prompt = f"{prompt}{sep}{preset.prompt_suffix}" if prompt else preset.prompt_suffix
    settings: dict[str, Any] = dict(preset.settings)
    settings.update(
        prompt=prompt,
        image_refs=[str(image)],
        resolution=fit_resolution(preset.settings.get("resolution", "480x512"), image_size),
        seed=int(seed),
        overrides=profile_override(preset.memory_profile),
    )
    return settings
