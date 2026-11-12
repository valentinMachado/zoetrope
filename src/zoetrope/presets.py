"""Presets: templates of Wan2GP settings, one JSON file per preset in `presets/`.

A preset holds the brief's fields (style, LoRA + weight, resolution, steps, cfg) plus what the
queue needs: its tier (preview or final), whether the model takes an end frame, its native fps,
the memory profile, and measured phase durations used for the time-remaining estimate until real
takes of the preset exist. A take snapshots the settings resolved here: editing a preset later
does not change past takes.
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
    return Preset(name=name, **data)


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
