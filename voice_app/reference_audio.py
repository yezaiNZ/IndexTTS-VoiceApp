"""Bounded audio import and explicit headphone-only reference preview."""

from __future__ import annotations

import io
import math
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from .reference_limits import (
    MAX_UPLOAD_BYTES,
    PCM_SAMPLE_BYTES,
    REFERENCE_RATE,
    WAV_HEADER_BYTES,
)
from .windows_audio import is_device_mapper

MAX_DECODED_BYTES = 64 * 1024 * 1024
SUPPORTED_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg"}


@dataclass(frozen=True)
class ReferenceAudio:
    wav: bytes
    pcm: bytes
    seconds: float


def _check_frames(frames: int, rate: int, channels: int) -> None:
    if frames < rate:
        seconds = frames / rate
        duration = f"{seconds:.2f}"
        if duration == "1.00":
            duration = f"{seconds:.5f}".rstrip("0")
        raise ValueError(
            f"检测到的音频时长为 {duration} 秒，不足 1 秒。"
            "请选择更长的片段，建议 5–15 秒清晰单人人声。"
        )
    if frames * channels * np.dtype("float32").itemsize > MAX_DECODED_BYTES:
        raise ValueError("音频解码后的数据量过大，请缩小文件或截取所需片段。")
    normalized_frames = (frames * REFERENCE_RATE + rate - 1) // rate
    if normalized_frames * PCM_SAMPLE_BYTES + WAV_HEADER_BYTES > MAX_UPLOAD_BYTES:
        raise ValueError("转换后的 WAV 超过 25 MB，请缩小文件或截取所需片段。")


def prepare_reference(content: bytes, filename: str) -> ReferenceAudio:
    import soundfile as sf

    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("请选择非空音频文件，文件大小不能超过 25 MB。")
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("支持 WAV、MP3、FLAC、OGG；请先将其他格式转换为 WAV。")
    try:
        with sf.SoundFile(io.BytesIO(content)) as source:
            rate = source.samplerate
            if not 8000 <= rate <= 96000 or source.channels not in (1, 2):
                raise ValueError("请使用 8–96 kHz 的单声道或双声道音频。")
            channels = source.channels
            _check_frames(source.frames, rate, channels)
            # Bound the allocation even for compressed files with extreme metadata.
            samples = source.read(source.frames, dtype="float32", always_2d=True)
    except (sf.SoundFileError, OSError) as exc:
        raise ValueError(
            "无法解码该音频，文件可能损坏或编码不受支持；请转换为 PCM WAV 后重试。"
        ) from exc
    _check_frames(len(samples), rate, channels)
    if not np.isfinite(samples).all():
        raise ValueError("音频包含无效采样值，请重新导出文件。")
    mono = samples.mean(axis=1)
    if np.max(np.abs(mono)) < 0.001:
        raise ValueError("音频几乎无声，或左右声道相互抵消；请换一段清晰的人声。")
    if rate != REFERENCE_RATE:
        divisor = math.gcd(rate, REFERENCE_RATE)
        mono = resample_poly(mono, REFERENCE_RATE // divisor, rate // divisor)
    pcm = np.rint(np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, PCM_SAMPLE_BYTES, REFERENCE_RATE, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    return ReferenceAudio(
        output.getvalue(), pcm, len(pcm) / (PCM_SAMPLE_BYTES * REFERENCE_RATE)
    )


class ReferencePreview:
    """Does not touch the microphone, model, mixer, or system defaults."""

    def __init__(self):
        self._lock = threading.Lock()
        self._router = None
        self._stopping = threading.Event()
        self._busy = False

    def play(self, reference: ReferenceAudio, device_id: int | None):
        from .audio import AudioRouter, list_output_devices, resolve_system_device

        devices = list_output_devices()
        if device_id is None:
            device_id = resolve_system_device(devices, input_device=False)
        device = next((item for item in devices if item.id == device_id), None)
        if (
            device is None
            or device.is_vb_cable
            or is_device_mapper(device.name)
        ):
            raise ValueError(
                "请明确选择耳机或扬声器试听，不能选择 VB-CABLE 或默认设备映射器。"
            )
        with self._lock:
            if self._busy:
                raise ValueError("参考音频正在试听，请先停止。")
            self._busy = True
            self._stopping.clear()
        router = None
        try:
            router = AudioRouter(None, True, device_id)
            with self._lock:
                self._router = router
                if self._stopping.is_set():
                    router.abort()
            # Small chunks keep explicit stop responsive without loading a model.
            for position in range(0, len(reference.pcm), 4410):
                if self._stopping.is_set():
                    break
                router.push(reference.pcm[position : position + 4410])
        finally:
            try:
                if router is not None:
                    router.close(drain=not self._stopping.is_set())
            finally:
                with self._lock:
                    self._router = None
                    self._busy = False

    def stop(self):
        with self._lock:
            self._stopping.set()
            if self._router is not None:
                self._router.abort()
