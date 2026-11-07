from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        self._lock = threading.RLock()

    @staticmethod
    def valid_name(value: str) -> str:
        name = unicodedata.normalize("NFC", value).strip()
        stem = name.split(".", 1)[0].upper()
        if (
            not name
            or len(name) > 60
            or name.startswith(".")
            or name.endswith(".")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or stem in {"CON", "PRN", "AUX", "NUL"}
            or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)
        ):
            raise ValueError(
                "角色名需为 1–60 字，不能使用路径、Windows 保留名称或特殊文件名字符。"
            )
        return name

    def _directory(self, name: str) -> Path:
        name = self.valid_name(name)
        path = self.root / name
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("不能操作链接到角色目录之外的预设。")
        return path

    def _unused_name(self, name: str, excluding: str | None = None):
        for path in self.root.iterdir() if self.root.exists() else ():
            if path.name.casefold() == name.casefold() and path.name != excluding:
                raise FileExistsError("已有同名角色，请换一个名字；不会覆盖原有预设。")

    @staticmethod
    def _write_json(directory: Path, data: dict):
        destination = directory / "preset.json"
        if destination.is_symlink():
            raise ValueError("不能覆盖链接形式的角色配置。")
        temporary = directory / (".preset-" + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, name: str, reference, language: str = "ZH") -> dict:
        name = self.valid_name(name)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._unused_name(name)
            destination = self._directory(name)
            with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as stage:
                directory = Path(stage)
                (directory / "prompt.wav").write_bytes(reference.wav)
                self._write_json(
                    directory,
                    {
                        "version": "1.0",
                        "prompt_audio": "prompt.wav",
                        "language": language,
                        "duration_seconds": reference.seconds,
                        "emo_control_method": 0,
                        "emo_weight": 0.65,
                        "emo_audio": "",
                        "advanced_params": {},
                    },
                )
                directory.rename(destination)
            return self.summary(name)

    def update(
        self, name: str, new_name: str, reference=None, language: str = "ZH"
    ) -> dict:
        new_name = self.valid_name(new_name)
        with self._lock:
            directory = self._directory(name)
            if not directory.is_dir() or not (directory / "preset.json").is_file():
                raise FileNotFoundError("角色不存在，请刷新角色列表。")
            self._unused_name(new_name, excluding=name)
            try:
                data = json.loads(
                    (directory / "preset.json").read_text(encoding="utf-8")
                )
                if not isinstance(data, dict):
                    raise TypeError("Invalid preset configuration")
            except (ValueError, TypeError):
                if reference is None:
                    raise ValueError(
                        "原角色配置损坏，请选择新的参考音频以修复预设。"
                    ) from None
                data = {
                    "version": "1.0",
                    "emo_control_method": 0,
                    "emo_weight": 0.65,
                    "advanced_params": {},
                }
            new_audio = "reference-" + uuid.uuid4().hex + ".wav" if reference else None
            destination = self._directory(new_name)
            moved = False
            try:
                if new_audio:
                    (directory / new_audio).write_bytes(reference.wav)
                    data.update(
                        prompt_audio=new_audio, duration_seconds=reference.seconds
                    )
                data["language"] = language
                if name != new_name:
                    # Older WebUI presets may store absolute paths inside their own
                    # directory; convert those to relative paths before moving it.
                    for key in ("prompt_audio", "emo_audio"):
                        value = data.get(key)
                        if isinstance(value, str) and value:
                            resolved = (directory / value).resolve()
                            if resolved.is_relative_to(directory):
                                data[key] = resolved.relative_to(directory).as_posix()
                    directory.rename(destination)
                    moved = True
                self._write_json(destination, data)
            except Exception:
                if moved:
                    destination.rename(directory)
                if new_audio:
                    (directory / new_audio).unlink(missing_ok=True)
                raise
            # Existing audio files are retained, not silently deleted on replacement.
            return self.summary(new_name)

    def trash(self, name: str) -> Path:
        with self._lock:
            directory = self._directory(name)
            if not directory.is_dir() or not (directory / "preset.json").is_file():
                raise FileNotFoundError("角色不存在，请刷新列表。")
            trash = self.root / ".trash"
            if trash.is_symlink() or trash.resolve() != trash.absolute():
                raise ValueError("角色回收目录不能是链接。")
            trash.mkdir(exist_ok=True)
            destination = trash / (name + "-" + uuid.uuid4().hex)
            directory.rename(destination)
            return destination

    def summary(self, name: str) -> dict:
        data = self.load(name) or {}
        audio = data.get("prompt_audio", "")
        seconds = data.get("duration_seconds")
        language = data.get("language")
        return {
            "name": name,
            "reference_audio": audio,
            "reference_exists": bool(audio) and Path(audio).is_file(),
            "emotion_mode": data.get("emo_control_method", 0)
            if isinstance(data.get("emo_control_method", 0), int)
            else 0,
            "language": language
            if isinstance(language, str) and language in {"ZH", "EN", "JA", "ES"}
            else "ZH",
            "duration_seconds": seconds
            if isinstance(seconds, (int, float))
            and math.isfinite(seconds)
            and seconds > 0
            else None,
        }

    def reference_bytes(self, name: str) -> bytes:
        from .reference_limits import MAX_UPLOAD_BYTES

        with self._lock:
            data = self.load(name)
            if not data or not data.get("prompt_audio"):
                raise FileNotFoundError("该角色没有参考音频，请通过管理角色重新导入。")
            path = Path(data["prompt_audio"])
            with path.open("rb") as source:
                result = source.read(MAX_UPLOAD_BYTES + 1)
            if len(result) > MAX_UPLOAD_BYTES:
                raise ValueError("参考音频超过 25 MB，请导入较短片段。")
            return result

    def list_names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            path.name
            for path in self.root.iterdir()
            if not path.name.startswith(".")
            and not path.is_symlink()
            and path.resolve() == path.absolute()
            and path.is_dir()
            and (path / "preset.json").is_file()
        )

    def load(self, name: str) -> dict | None:
        if name not in self.list_names():
            return None
        preset_dir = self.root / name
        try:
            data = json.loads((preset_dir / "preset.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        for key in ("prompt_audio", "emo_audio"):
            if isinstance(data.get(key), str) and data[key]:
                try:
                    data[key] = str((preset_dir / data[key]).resolve())
                except (OSError, ValueError):
                    data[key] = ""
            else:
                data[key] = ""
        return data


def preset_summaries(store: PresetStore) -> list[PresetSummary]:
    return [PresetSummary(**store.summary(name)) for name in store.list_names()]


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
