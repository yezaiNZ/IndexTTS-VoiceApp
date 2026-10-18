from __future__ import annotations

import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .audio import AudioRouter, MicrophoneMixer, SAMPLE_RATE
from .presets import PresetResolver, PresetStore, ResolvedSynthesis
from .output_store import GeneratedAudioStore
from .schemas import SynthesisRequest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("VOICE_APP_DATA_DIR", PROJECT_ROOT))
OUTPUT_DIR = DATA_ROOT / "outputs"
DEFAULT_INDEXTTS_ROOT = Path(os.environ.get("INDEXTTS_ROOT", ""))


class RuntimeBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeOptions:
    index_tts_root: Path = DEFAULT_INDEXTTS_ROOT
    model_dir: Path | None = None
    presets_dir: Path = DATA_ROOT / "presets"
    device: str | None = None
    prefer_half: bool = True
    use_deepspeed: bool = False
    use_cuda_kernel: bool = False
    use_accel: bool = False
    use_torch_compile: bool = False
    use_qwen_emo: bool = False

    @property
    def resolved_model_dir(self) -> Path:
        return self.model_dir or self.index_tts_root / "checkpoints"


class ModelRuntime:
    """Owns exactly one IndexTTS model and serializes GPU inference."""

    def __init__(
        self,
        options: RuntimeOptions | None = None,
        resolver: PresetResolver | None = None,
        output_store: GeneratedAudioStore | None = None,
    ):
        self.options = options or RuntimeOptions()
        self.resolver = resolver or PresetResolver(
            PresetStore(self.options.presets_dir)
        )
        self._model = None
        self._load_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._loading = False
        self._load_error: str | None = None
        self._active: SynthesisSession | None = None
        self.mixer = MicrophoneMixer()
        self.output_store = output_store or GeneratedAudioStore(OUTPUT_DIR / "auto-saved")

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def status(self) -> dict:
        with self._state_lock:
            active = self._active
            return {
                "model_loaded": self.loaded,
                "model_loading": self._loading,
                "load_error": self._load_error,
                "busy": active is not None,
                "job_id": active.job_id if active else None,
                "stop_requested": active.stop_event.is_set() if active else False,
                "device": self.options.device,
                "index_tts_root": str(self.options.index_tts_root),
                "model_dir": str(self.options.resolved_model_dir),
                "presets_dir": str(self.options.presets_dir),
                "mixer": self.mixer.status(),
            }

    def ensure_loaded(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            with self._state_lock:
                self._loading = True
                self._load_error = None
            try:
                import torch

                index_tts_root = self.options.index_tts_root.expanduser().resolve()
                if not (index_tts_root / "indextts" / "infer_v2_5.py").is_file():
                    raise FileNotFoundError(
                        f"IndexTTS 2.5 source not found: {index_tts_root}"
                    )
                source_path = str(index_tts_root)
                if source_path not in sys.path:
                    sys.path.insert(0, source_path)

                from indextts.infer_v2_5 import IndexTTS2

                model_dir = self.options.resolved_model_dir.expanduser().resolve()
                config_path = model_dir / "config.yaml"
                if not config_path.is_file():
                    raise FileNotFoundError(f"model config not found: {config_path}")
                use_bf16 = bool(
                    self.options.prefer_half
                    and torch.cuda.is_available()
                    and torch.cuda.is_bf16_supported()
                )
                self._model = IndexTTS2(
                    cfg_path=str(config_path),
                    model_dir=str(model_dir),
                    use_bf16=use_bf16,
                    device=self.options.device,
                    use_cuda_kernel=self.options.use_cuda_kernel,
                    use_deepspeed=self.options.use_deepspeed,
                    use_accel=self.options.use_accel,
                    use_torch_compile=self.options.use_torch_compile,
                    use_qwen_emo=self.options.use_qwen_emo,
                )
                return self._model
            except Exception as exc:
                with self._state_lock:
                    self._load_error = str(exc)
                raise
            finally:
                with self._state_lock:
                    self._loading = False

    def start_session(self, request: SynthesisRequest) -> "SynthesisSession":
        resolved = self.resolver.resolve(request)
        if resolved.emotion_mode == 3 and not self.options.use_qwen_emo:
            raise ValueError(
                "emotion text mode requires starting the service with --qwen-emo"
            )
        model = self.ensure_loaded()
        if not self._generation_lock.acquire(blocking=False):
            raise RuntimeBusyError("another synthesis job is already running")
        try:
            session = SynthesisSession(self, model, resolved)
            with self._state_lock:
                self._active = session
            return session
        except Exception:
            self._generation_lock.release()
            raise

    def stop(self) -> tuple[bool, str | None]:
        with self._state_lock:
            session = self._active
        if session is None:
            return False, None
        session.request_stop()
        return True, session.job_id

    def _finish(self, session: "SynthesisSession"):
        with self._state_lock:
            if self._active is session:
                self._active = None
        self._generation_lock.release()


class SynthesisSession:
    def __init__(self, runtime, model, resolved: ResolvedSynthesis):
        self.runtime: ModelRuntime = runtime
        self.model = model
        self.resolved = resolved
        self.job_id = uuid.uuid4().hex
        self.output_path = runtime.output_store.path_for(self.job_id)
        self.stop_event = threading.Event()
        self.using_mixer = runtime.mixer.running
        self.router = AudioRouter(
            None if self.using_mixer else resolved.output_device,
            resolved.monitor_enabled,
            resolved.monitor_device,
        )
        self._closed = False

    def request_stop(self):
        self.stop_event.set()
        if self.using_mixer:
            self.runtime.mixer.clear_tts()
        self.router.abort()

    def iter_pcm(self) -> Iterator[bytes]:
        recording = None
        generator = None
        stopped = False
        complete = False
        try:
            if self.resolved.save_output:
                recording = self.runtime.output_store.begin(self.job_id, SAMPLE_RATE)

            generation = dict(self.resolved.generation)
            max_text_tokens = int(generation.pop("max_text_tokens_per_segment"))
            duration_factor = float(generation.pop("duration_factor"))
            interval_silence = int(generation.pop("interval_silence"))
            if generation.get("top_k") == 0:
                generation["top_k"] = None

            emotion_vector = self.resolved.emotion_vector
            if emotion_vector is not None:
                emotion_vector = self.model.normalize_emo_vec(
                    emotion_vector, apply_bias=True
                )

            generator = self.model.infer(
                spk_audio_prompt=self.resolved.reference_audio,
                text=self.resolved.text,
                output_path=None,
                lang=self.resolved.language,
                emo_audio_prompt=self.resolved.emotion_audio,
                emo_alpha=self.resolved.emotion_weight,
                emo_vector=emotion_vector,
                use_emo_text=self.resolved.emotion_mode == 3,
                emo_text=self.resolved.emotion_text or None,
                use_random=self.resolved.emotion_random,
                interval_silence=interval_silence,
                max_text_tokens_per_segment=max_text_tokens,
                stream_return=True,
                duration_factor=duration_factor,
                **generation,
            )
            for tensor in generator:
                if self.stop_event.is_set():
                    stopped = True
                    break
                pcm = (
                    tensor.detach()
                    .cpu()
                    .clamp(-32767, 32767)
                    .to(dtype=_torch_int16())
                    .contiguous()
                    .numpy()
                    .reshape(-1)
                    .astype("<i2", copy=False)
                    .tobytes()
                )
                if recording is not None:
                    recording.write(pcm)
                if self.using_mixer:
                    self.runtime.mixer.inject(pcm)
                self.router.push(pcm)
                yield pcm
            complete = not stopped and not self.stop_event.is_set()
        finally:
            try:
                if generator is not None:
                    generator.close()
            finally:
                try:
                    self.close(drain=complete)
                finally:
                    if recording is not None:
                        recording.finish(complete)
                    else:
                        self.runtime.output_store.remember(self.job_id, {
                            "save_requested": False, "saved": False,
                            "path": None, "error": None, "removed_count": 0,
                        })

    def close(self, drain: bool = True):
        if self._closed:
            return
        self._closed = True
        try:
            if drain and self.using_mixer:
                if not self.runtime.mixer.wait_for_tts():
                    raise RuntimeError(
                        "timed out waiting for mixed TTS audio to finish"
                    )
            self.router.close(drain=drain)
        finally:
            self.runtime._finish(self)


def _torch_int16():
    import torch

    return torch.int16
