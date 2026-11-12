"""Where Zoetrope keeps its files. Everything generated lives under `outputs/` (not tracked).

The data root can be moved with the ZOETROPE_OUTPUTS environment variable (the tests do).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRESETS_DIR = ROOT / "presets"
LORAS_DIR = ROOT / "loras"
WAN2GP_DIR = ROOT / "tools" / "Wan2GP"


def outputs_dir() -> Path:
    return Path(os.environ.get("ZOETROPE_OUTPUTS", ROOT / "outputs"))


def db_path() -> Path:
    return outputs_dir() / "zoetrope.db"


def jobs_dir() -> Path:
    return outputs_dir() / "jobs"


def job_dir(job_id: int) -> Path:
    return jobs_dir() / str(job_id)


def keyframes_dir(job_id: int) -> Path:
    return job_dir(job_id) / "keyframes"


def takes_dir(job_id: int) -> Path:
    return job_dir(job_id) / "takes"


def assembly_dir(job_id: int) -> Path:
    return job_dir(job_id) / "assembly"


def wan2gp_output_dir() -> Path:
    """Where Wan2GP writes; the worker moves each result into its take's folder."""
    return outputs_dir() / "wan2gp"


def to_url(path: str | Path | None) -> str | None:
    """Public URL of a file under outputs/jobs, served by the API at /files."""
    if not path:
        return None
    rel = Path(path).resolve().relative_to(jobs_dir().resolve())
    return "/files/" + rel.as_posix()
