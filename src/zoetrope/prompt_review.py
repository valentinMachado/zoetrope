"""Prompt review: what a motion prompt (a segment) or an image prompt (a keygen) is missing.

Two levels, decided in P12:

- `check`: instant rules (language, style named, camera said, number of actions for the duration,
  negations, length). The API runs them as the user types; they return codes, the front words them
  (in French) and says how to fix each one.
- the AI review: Qwen3 4B, already on disk as Flux 2 Klein's text encoder, run by the worker as a
  `review` task of the queue (~30 s, 5 GB of VRAM, the video model is released first when it does
  not fit). It reads the prompt and its context given as text (duration, end image, how the images
  were created; it does not see the images) and replies with what is missing and a corrected
  prompt, which the user may take or not. `build_messages` and `parse_reply` are pure; `Reviewer`
  needs torch and Wan2GP's mmgp, so only the worker builds one.

Measured in P12 (docs/benchmark.md, "Prompt review"): the rewrite is good, the list of what is
missing is weaker (generic points, sometimes wrong), hence the rules beside it.

Shared by the API and the worker: Python 3.10-compatible, no FastAPI, no torch at import.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TARGETS = ("motion", "image")

# --- rules --------------------------------------------------------------------------------------

FRENCH_WORDS = {
    "le", "la", "les", "des", "du", "un", "une", "et", "puis", "qui", "que", "avec", "dans", "sur", "pour",
    "est", "sont", "se", "sa", "son", "ses", "au", "aux", "de", "ne", "pas", "elle", "il", "ils", "elles",
    "ensuite", "avant", "après", "vers", "très", "lentement", "doucement", "fait", "visage", "yeux",
    "caméra", "fond", "blanc", "homme", "femme", "sourit", "tourne", "regarde", "tous", "sens", "mais",
}
ENGLISH_WORDS = {
    "the", "a", "an", "and", "then", "of", "with", "in", "on", "her", "his", "she", "he", "is", "are",
    "slowly", "gently", "camera", "while", "into", "to", "from", "make", "face", "eyes", "background",
    "smiles", "turns", "looks", "it", "its", "their", "they", "at", "by", "static",
}
# Words that name a visual style (English and French, accents stripped).
STYLE_WORDS = (
    "watercolor", "watercolour", "painting", "painted", "paint", "brushstroke", "oil", "gouache", "acrylic",
    "pastel", "charcoal", "pencil", "sketch", "drawing", "drawn", "ink", "illustration", "illustrated", "comic",
    "cartoon", "anime", "manga", "photo", "photograph", "photographic", "photorealistic", "realistic", "cinematic",
    "film", "3d", "render", "clay", "pixel", "engraving", "woodcut", "lineart", "line art", "style",
    "aquarelle", "peinture", "peint", "crayon", "dessin", "dessine", "encre", "bande dessinee", "photographie",
    "realiste", "fusain",
)
CAMERA_WORDS = (
    "camera", "static", "still shot", "fixed shot", "locked", "zoom", "pan", "panning", "tilt", "dolly", "push in",
    "push-in", "pull back", "pull-back", "tracking", "orbit", "handheld", "close-up", "close up", "wide shot",
    "camera fixe", "plan fixe", "travelling", "zoome", "panoramique", "gros plan",
)
# What chains one action to the next.
SEQUENCE_MARKERS = (
    r"\bthen\b", r"\bbefore\b", r"\bafter(?:wards)?\b", r"\bfinally\b", r"\bnext\b", r"\buntil\b",
    r"\bpuis\b", r"\bensuite\b", r"\bavant de\b", r"\bapres\b", r"\benfin\b", r"\bjusqu'a\b",
)
NEGATIONS = (r"\bno\b", r"\bnot\b", r"\bwithout\b", r"\bdon't\b", r"\bnever\b", r"\bpas de\b", r"\bsans\b",
             r"\bne \w+ pas\b", r"\bjamais\b")
SHORT_WORDS = 5


@dataclass(frozen=True)
class Context:
    """What the review knows besides the text. For a motion prompt: the segment's duration and
    end image; for an image prompt: its target position and sources. `notes` are facts given to
    the AI as text (e.g. the instruction a keyframe was created with)."""

    target: str  # "motion" or "image"
    duration_s: float | None = None
    end_image: str | None = None  # motion: None, "exact" (the final reaches it) or "guide"
    position: int | None = None  # image: 0 = the start image
    sources: tuple[str, ...] = ()  # image: "start", "keyframe" or "upload", the first is the main scene
    notes: tuple[str, ...] = field(default_factory=tuple)
    style: str | None = None  # the job's style, put in front of the prompt at render time

    def to_json(self) -> dict[str, Any]:
        return {"target": self.target, "duration_s": self.duration_s, "end_image": self.end_image,
                "position": self.position, "sources": list(self.sources), "notes": list(self.notes),
                "style": self.style}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Context:
        return cls(target=d["target"], duration_s=d.get("duration_s"), end_image=d.get("end_image"),
                   position=d.get("position"), sources=tuple(d.get("sources") or ()),
                   notes=tuple(d.get("notes") or ()), style=d.get("style"))


def _plain(text: str) -> str:
    """Lower case, accents stripped, apostrophes straightened."""
    text = unicodedata.normalize("NFKD", text.replace("’", "'").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _words(plain: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", plain)


def _has_any(plain: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(t)}", plain) for t in terms)


def is_french(text: str) -> bool:
    words = re.findall(r"[a-zà-ÿ']+", text.replace("’", "'").lower())
    fr = sum(w in FRENCH_WORDS or w.startswith(("l'", "d'", "qu'")) for w in words)
    en = sum(w in ENGLISH_WORDS for w in words)
    accents = len(re.findall(r"[éèêàùçôîâû]", text.lower()))
    return fr + min(accents, 3) > en and fr + accents > 0


def count_actions(text: str) -> int:
    """Actions chained in sequence: 1 + the markers that chain them (then, puis, avant de…)."""
    plain = _plain(text)
    return 1 + sum(len(re.findall(m, plain)) for m in SEQUENCE_MARKERS)


def max_actions(duration_s: float | None) -> int:
    """One or two movements per shot (MOTION_TIPS, measured in P7-P9); a third past 5 s."""
    return 3 if duration_s and duration_s > 5 else 2


def check(text: str, ctx: Context) -> list[dict[str, Any]]:
    """Instant rules. Each finding is {"code", ...params}; the front words it."""
    findings: list[dict[str, Any]] = []
    text = text.strip()
    plain = _plain(text)
    words = _words(plain)
    edit = ctx.target == "image" and bool(ctx.sources)
    if not text:
        if not edit:
            findings.append({"code": "empty"})
        return findings
    if is_french(text):
        findings.append({"code": "french"})
    if len(words) < SHORT_WORDS and not edit:
        findings.append({"code": "short", "words": len(words)})
    # An edit keeps the style of its source image; a motion prompt or an image from text alone must
    # name it, unless the job's style is put in front of it.
    if not edit and not ctx.style and not _has_any(plain, STYLE_WORDS):
        findings.append({"code": "no_style"})
    if ctx.target == "motion":
        if not _has_any(plain, CAMERA_WORDS):
            findings.append({"code": "no_camera"})
        n, allowed = count_actions(text), max_actions(ctx.duration_s)
        if n > allowed:
            findings.append({"code": "too_many_actions", "actions": n, "max": allowed,
                             "duration_s": ctx.duration_s})
    if any(re.search(p, plain) for p in NEGATIONS):
        findings.append({"code": "negation"})
    return findings


# --- the AI review ------------------------------------------------------------------------------

# The texts the AI gets, one line per entry (kept as measured in P12: changing a word changes replies).
SYSTEM_MOTION = "\n".join([
    "You review prompts for an image-to-video model (Wan 2.2). "
    "The video starts from a given still image and the prompt describes what moves.",
    "A good motion prompt:",
    "- is in English;",
    "- names the visual style of the image when it is not a photo (e.g. \"a watercolor painting, loose wet "
    "brushstrokes, paper texture\"), otherwise the video turns photorealistic;",
    "- names the subject in a few words, then what moves, in order;",
    "- has one or two movements for 2-5 seconds, not a story;",
    "- says what the camera does (\"static camera\", \"slow push-in\") and what stays still;",
    "- says what to show, not what to avoid (no negations);",
    "- with an end image, describes the path towards it.",
])

SYSTEM_IMAGE = "\n".join([
    "You review prompts for an image model (Flux 2 Klein) that creates a keyframe for a video, "
    "from a text and/or source images.",
    "A good image prompt:",
    "- is in English;",
    "- with source images, is an imperative edit instruction saying only what changes "
    "(\"Make her close her eyes and smile widely\"); the rest of the image is kept;",
    "- from text alone, describes the whole image: subject, framing, background and style "
    "(\"a watercolor portrait of...\");",
    "- with several source images, says the role of each one (\"Put the cat from the second image on her lap\");",
    "- for a keyframe, stays close to the start image: the video model must reach it in 2-3 seconds;",
    "- describes expressions and gaze rather than head or body movements, which are poorly followed.",
])

REPLY_FORMAT = "\n".join([
    "Checks were run on the prompt; their findings are listed and certain. "
    "Rewrite the prompt so that it fixes every one of them, and nothing else:",
    "- translate faithfully: keep each action, object and detail the author wrote, "
    "with its exact meaning (a wink stays a wink);",
    "- the only actions you may drop are those beyond the maximum the checks give;",
    "- add nothing the author did not write, except the style, the camera and what stays still "
    "when the checks ask for them;",
    "- take the style from the context when it gives one; otherwise write [style] where it goes.",
    "Then, only if you see a real problem the checks cannot see (an action the model cannot show, "
    "a contradiction with the end image, an edit that changes too much), name it in one short French sentence; "
    "most prompts have none.",
    "Reply in exactly this format and nothing else:",
    "PROMPT:",
    "<the rewritten prompt, in English, one paragraph>",
    "OTHER:",
    "- <the problem in French, or \"- rien\">",
])

# What each rule finding tells the AI.
FACTS = {
    "empty": "The prompt is empty.",
    "french": "The prompt is not in English.",
    "short": "The prompt is very short.",
    "no_style": "The prompt does not name the visual style.",
    "no_camera": "The prompt does not say what the camera does.",
    "too_many_actions": "{actions} actions in sequence for {duration_s:g} s: at most {max}; keep the first ones.",
    "negation": "The prompt uses a negation: say what to show instead.",
}

# One worked example each: a 4B model follows a shown answer better than a described one.
EXAMPLE_MOTION = (
    "Duration: 3 s.\nNo end image: the prompt alone drives the motion.\n"
    "Checks: The prompt is not in English. The prompt does not name the visual style. "
    "The prompt does not say what the camera does.\n"
    "Prompt to review: l'homme sourit et hoche la tête",
    "PROMPT:\n[style] of a man. He smiles warmly and nods. Static camera.\nOTHER:\n- rien",
)
EXAMPLE_IMAGE = (
    "Target: keyframe 1 (an image the video must reach).\n"
    "Source images: 1. the start image (main scene, edited).\n"
    "Checks: The prompt is not in English.\n"
    "Prompt to review: elle sourit et elle se lève et marche vers la porte",
    "PROMPT:\nMake her smile, stand up and walk towards the door.\n"
    "OTHER:\n- Se lever et marcher change trop l'image : la vidéo ne pourra pas y arriver en 2 à 3 s.",
)
CJK = re.compile("[\\u3000-\\u9fff\\uac00-\\ud7af\\uff00-\\uffef]+")
STYLE_PLACEHOLDER = "[style]"


def describe_context(ctx: Context) -> list[str]:
    lines = []
    if ctx.target == "motion":
        if ctx.duration_s:
            lines.append(f"Duration: {ctx.duration_s:g} s.")
        if ctx.end_image == "exact":
            lines.append("There is an end image: the final render reaches it exactly.")
        elif ctx.end_image == "guide":
            lines.append("There is an indicative end image (not sent to the model).")
        else:
            lines.append("No end image: the prompt alone drives the motion.")
    else:
        lines.append("Target: the start image of the video." if ctx.position == 0
                     else f"Target: keyframe {ctx.position} (an image the video must reach).")
        if not ctx.sources:
            lines.append("No source image: the image comes from the text alone.")
        else:
            names = {"start": "the start image", "keyframe": "another keyframe of the video",
                     "upload": "an uploaded image"}
            lines.append("Source images: " + "; ".join(
                f"{i + 1}. {names.get(s, s)}" + (" (main scene, edited)" if i == 0 else "")
                for i, s in enumerate(ctx.sources)) + ".")
    if ctx.style:
        lines.append(f"The visual style is put in front of the prompt automatically: \"{ctx.style}\". "
                     "Do not repeat it.")
    lines += list(ctx.notes)
    return lines


def describe_findings(findings: list[dict[str, Any]]) -> str:
    facts = [FACTS[f["code"]].format(**{"duration_s": 0, **{k: v for k, v in f.items() if v is not None}})
             for f in findings if f["code"] in FACTS]
    return "Checks: " + (" ".join(facts) if facts else "nothing found.")


def build_messages(text: str, ctx: Context) -> list[dict[str, str]]:
    system = (SYSTEM_MOTION if ctx.target == "motion" else SYSTEM_IMAGE) + "\n\n" + REPLY_FORMAT
    example = EXAMPLE_MOTION if ctx.target == "motion" else EXAMPLE_IMAGE
    user = "\n".join([*describe_context(ctx), describe_findings(check(text, ctx)),
                      f"Prompt to review: {text.strip() or '(empty)'}"])
    return [{"role": "system", "content": system}, {"role": "user", "content": example[0]},
            {"role": "assistant", "content": example[1]}, {"role": "user", "content": user}]


def parse_reply(reply: str) -> dict[str, Any]:
    """{"issues": [str], "suggestion": str}: the lines after PROMPT and after OTHER (colons
    optional, either order); a reply off the format is all suggestion. Stray CJK characters (Qwen
    sometimes slips one in) are dropped."""
    reply = CJK.sub("", reply).strip()
    parts = re.split(r"^\s*(PROMPT|OTHER|MISSING)\s*:?[ \t]*$", reply, flags=re.M)
    if len(parts) < 3:
        return {"issues": [], "suggestion": " ".join(reply.split())}
    sections = {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    issues = []
    for line in (sections.get("OTHER") or sections.get("MISSING") or "").splitlines():
        line = line.strip().lstrip("-*•").strip()
        if line and _plain(line).strip(" .") not in ("rien", "nothing", "none"):
            issues.append(line)
    suggestion = " ".join((sections.get("PROMPT") or "").split()).strip().strip('"').strip()
    suggestion = re.sub(r"\s+([.,;:!?])", r"\1", suggestion)
    # "[style] of a watercolor painting": the model kept the placeholder and named the style anyway.
    if STYLE_PLACEHOLDER in suggestion and _has_any(_plain(suggestion.replace(STYLE_PLACEHOLDER, "")), STYLE_WORDS):
        suggestion = re.sub(r"\[style\](?:\s+of)?\s*", "", suggestion).strip()
        suggestion = suggestion[:1].upper() + suggestion[1:]
    return {"issues": issues, "suggestion": suggestion}


class Reviewer:
    """Qwen3 4B (models/Qwen3, int8) loaded on demand, freed after each review so that the video
    models get the memory back. Worker only: needs torch and Wan2GP's mmgp on sys.path.

    On Pascal the int8 weights only run with bf16 activations (quanto refuses fp32): slow but fine
    for a reply of ~50 tokens (~4.5 tokens/s, 10-15 s with the load, P12)."""

    MAX_NEW_TOKENS = 200  # replies measured at 24-60 tokens
    VRAM_NEEDED = 5.5 * 2**30  # measured peak 4.96 GiB

    def __init__(self, model_dir: Any):
        self.model_dir = model_dir

    def review(self, text: str, ctx: Context, should_stop: Callable[[], bool] = lambda: False,
               on_phase: Callable[[str], None] = lambda _phase: None) -> dict[str, Any]:
        """{"issues", "suggestion", "raw", "durations"}. `should_stop` is polled every second
        during generation; `on_phase` gets loading_model, then inference."""
        import time

        import torch
        from mmgp import offload
        from transformers import AutoTokenizer, StoppingCriteria, StoppingCriteriaList

        t0 = time.time()
        on_phase("loading_model")
        model = offload.fast_load_transformers_model(
            str(self.model_dir / "qwen3_quanto_bf16_int8.safetensors"),
            defaultConfigPath=str(self.model_dir / "config.json"),
            configKwargs={"attn_implementation": "sdpa"}, writable_tensors=False, default_dtype=torch.bfloat16)
        try:
            for p in model.parameters():
                if type(p.data) is torch.Tensor and p.dtype == torch.float32:
                    p.data = p.data.to(torch.bfloat16)
            model.eval().to("cuda")
            tok = AutoTokenizer.from_pretrained(str(self.model_dir))
            prompt = tok.apply_chat_template(build_messages(text, ctx), tokenize=False, add_generation_prompt=True,
                                             enable_thinking=False)
            ids = tok(prompt, return_tensors="pt").to("cuda")
            loaded = time.time()
            on_phase("inference")

            class _Stop(StoppingCriteria):
                last = 0.0

                def __call__(self, *_args: Any, **_kwargs: Any) -> bool:
                    now = time.time()
                    if now - self.last < 1.0:
                        return False
                    self.last = now
                    return should_stop()

            with torch.no_grad():
                out = model.generate(**ids, max_new_tokens=self.MAX_NEW_TOKENS, do_sample=False,
                                     pad_token_id=tok.eos_token_id, stopping_criteria=StoppingCriteriaList([_Stop()]))
            new = out[0, ids["input_ids"].shape[1]:]
            reply = tok.decode(new, skip_special_tokens=True)
            done = time.time()
            return {**parse_reply(reply), "raw": reply,
                    "durations": {"loading_model": round(loaded - t0, 1), "inference": round(done - loaded, 1),
                                  "tokens": int(new.shape[0]), "total": round(done - t0, 1)}}
        finally:
            del model
            import gc

            gc.collect()
            torch.cuda.empty_cache()
