from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .schemas import PresetSummary, SynthesisRequest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRESETS_DIR = PROJECT_ROOT / "presets"


DEFAULT_GENERATION: dict[str, Any] = {
    "do_sample": True,
    "top_p": 0.8,
    "top_k": 30,
    "temperature": 0.8,
    "length_penalty": 0.0,
    "num_beams": 3,
    "repetition_penalty": 10.0,
    "max_mel_tokens": 1500,
    "max_text_tokens_per_segment": 120,
    "duration_factor": 1.0,
    "interval_silence": 200,
}


@dataclass(frozen=True)
class ResolvedSynthesis:
    text: str
    reference_audio: str
    language: str
    emotion_mode: int
    emotion_audio: str | None
    emotion_weight: float
    emotion_vector: list[float] | None
    emotion_text: str | None
    emotion_random: bool
    output_device: int | None
    monitor_enabled: bool
    monitor_device: int | None
    save_output: bool
    generation: dict[str, Any]


class PresetStore:
    """Read presets that use the IndexTTS WebUI storage format."""

    def __init__(self, root: Path = DEFAULT_PRESETS_DIR):
        self.root = Path(root).expanduser().resolve()

    def list_names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            path.name
            for path in self.root.iterdir()
            if path.is_dir() and (path / "preset.json").is_file()
        )

    def load(self, name: str) -> dict | None:
        if name not in self.list_names():
            return None
        preset_dir = self.root / name
        try:
            data = json.loads((preset_dir / "preset.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        for key in ("prompt_audio", "emo_audio"):
            if data.get(key):
                data[key] = str((preset_dir / data[key]).resolve())
        return data


def preset_summaries(store: PresetStore) -> list[PresetSummary]:
    result: list[PresetSummary] = []
    for name in store.list_names():
        data = store.load(name) or {}
        result.append(
            PresetSummary(
                name=name,
                reference_audio=data.get("prompt_audio", ""),
                emotion_mode=int(data.get("emo_control_method", 0)),
            )
        )
    return result


class PresetResolver:
    def __init__(
        self,
        store: PresetStore | None = None,
        loader: Callable[[str], dict | None] | None = None,
    ):
        self.store = store or PresetStore()
        self.loader = loader or self.store.load

    def resolve(self, request: SynthesisRequest) -> ResolvedSynthesis:
        text = request.text.strip()
        if not text:
            raise ValueError("text cannot be blank")
        preset: dict[str, Any] = {}
        if request.preset:
            loaded = self.loader(request.preset)
            if loaded is None:
                raise ValueError(f"preset does not exist: {request.preset}")
            preset = loaded

        reference_audio = request.reference_audio or preset.get("prompt_audio")
        if not reference_audio:
            raise ValueError("the selected preset has no reference audio")
        reference_path = Path(reference_audio).expanduser().resolve()
        if not reference_path.is_file():
            raise ValueError(f"reference audio does not exist: {reference_path}")

        emotion_mode = _coalesce(
            request.emotion_mode, preset.get("emo_control_method"), 0
        )
        emotion_audio = request.emotion_audio or preset.get("emo_audio") or None
        if emotion_audio:
            emotion_path = Path(emotion_audio).expanduser().resolve()
            if not emotion_path.is_file():
                raise ValueError(f"emotion audio does not exist: {emotion_path}")
            emotion_audio = str(emotion_path)
        if emotion_mode == 1 and not emotion_audio:
            raise ValueError("emotion audio mode requires emotion_audio")

        vector = (
            request.emotion_vector
            if request.emotion_vector is not None
            else preset.get("emo_vector")
        )
        if emotion_mode != 2:
            vector = None
        elif vector is None or len(vector) != 8:
            raise ValueError("emotion vector mode requires exactly 8 values")

        advanced = dict(DEFAULT_GENERATION)
        advanced.update(preset.get("advanced_params") or {})
        overrides = request.generation.model_dump(exclude_none=True)
        advanced.update(overrides)

        return ResolvedSynthesis(
            text=text,
            reference_audio=str(reference_path),
            language=request.language,
            emotion_mode=int(emotion_mode),
            emotion_audio=emotion_audio if emotion_mode == 1 else None,
            emotion_weight=float(
                _coalesce(request.emotion_weight, preset.get("emo_weight"), 0.65)
            ),
            emotion_vector=list(vector) if vector is not None else None,
            emotion_text=_coalesce(request.emotion_text, preset.get("emo_text"), None),
            emotion_random=bool(
                _coalesce(request.emotion_random, preset.get("emo_random"), False)
            ),
            output_device=request.output_device,
            monitor_enabled=request.monitor_enabled,
            monitor_device=request.monitor_device,
            save_output=request.save_output,
            generation=advanced,
        )


def _coalesce(*values):
    for value in values:
        if value is not None:
            return value
    return None
