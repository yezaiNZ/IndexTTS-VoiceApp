from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from .schemas import AudioDevice


SAMPLE_RATE = 22050


def list_output_devices() -> list[AudioDevice]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    default_output = _default_output_device()
    result: list[AudioDevice] = []
    for index, device in enumerate(devices):
        channels = int(device.get("max_output_channels", 0))
        if channels < 1:
            continue
        host_index = int(device.get("hostapi", 0))
        host_name = str(host_apis[host_index].get("name", ""))
        name = str(device.get("name", f"Device {index}"))
        normalized = name.casefold()
        result.append(
            AudioDevice(
                id=index,
                name=name,
                host_api=host_name,
                channels=channels,
                default_samplerate=float(device.get("default_samplerate", SAMPLE_RATE)),
                is_default=index == default_output,
                is_vb_cable=("cable input" in normalized or "vb-audio" in normalized),
            )
        )
    return result


def list_input_devices() -> list[AudioDevice]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    default_input = _default_input_device()
    result: list[AudioDevice] = []
    for index, device in enumerate(devices):
        channels = int(device.get("max_input_channels", 0))
        if channels < 1:
            continue
        host_index = int(device.get("hostapi", 0))
        host_name = str(host_apis[host_index].get("name", ""))
        name = str(device.get("name", f"Device {index}"))
        normalized = name.casefold()
        result.append(
            AudioDevice(
                id=index,
                name=name,
                host_api=host_name,
                channels=channels,
                default_samplerate=float(device.get("default_samplerate", SAMPLE_RATE)),
                is_default=index == default_input,
                is_vb_cable=("cable output" in normalized or "vb-audio" in normalized),
            )
        )
    return result


def _default_input_device() -> int | None:
    try:
        value = sd.default.device[0]
        return int(value) if value is not None and int(value) >= 0 else None
    except (TypeError, ValueError, sd.PortAudioError):
        return None


def _default_output_device() -> int | None:
    try:
        value = sd.default.device[1]
        return int(value) if value is not None and int(value) >= 0 else None
    except (TypeError, ValueError, sd.PortAudioError):
        return None


@dataclass
class _Sink:
    device_id: int
    source_rate: int = SAMPLE_RATE

    def __post_init__(self):
        info = sd.query_devices(self.device_id)
        self.target_rate = int(float(info["default_samplerate"]))
        self.channels = 2 if int(info["max_output_channels"]) >= 2 else 1
        self.items: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self.stopping = threading.Event()
        self.error: Exception | None = None
        self.stream = sd.OutputStream(
            device=self.device_id,
            samplerate=self.target_rate,
            channels=self.channels,
            dtype="int16",
            latency="high",
        )
        self.stream.start()
        self.worker = threading.Thread(
            target=self._run,
            name=f"audio-device-{self.device_id}",
            daemon=True,
        )
        self.worker.start()

    def put(self, pcm: bytes):
        if self.error is not None:
            raise RuntimeError(
                f"audio output device {self.device_id} failed: {self.error}"
            ) from self.error
        if not self.stopping.is_set():
            self.items.put(pcm)

    def _run(self):
        try:
            while not self.stopping.is_set():
                item = self.items.get()
                if item is None:
                    break
                samples = convert_pcm(
                    item,
                    source_rate=self.source_rate,
                    target_rate=self.target_rate,
                    channels=self.channels,
                )
                self.stream.write(samples)
        except Exception as exc:  # surfaced on the next push or at close
            self.error = exc
        finally:
            try:
                self.stream.stop()
            except sd.PortAudioError:
                pass
            self.stream.close()

    def close(self, drain: bool = True):
        if not drain:
            self.abort()
        else:
            self.items.put(None)
        self.worker.join(timeout=5)
        if self.error is not None and drain:
            raise RuntimeError(
                f"audio output device {self.device_id} failed: {self.error}"
            ) from self.error

    def abort(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        try:
            self.stream.abort()
        except sd.PortAudioError:
            pass
        try:
            self.items.put_nowait(None)
        except queue.Full:
            while True:
                try:
                    self.items.get_nowait()
                except queue.Empty:
                    break
            self.items.put_nowait(None)


def convert_pcm(
    pcm: bytes,
    source_rate: int,
    target_rate: int,
    channels: int,
) -> np.ndarray:
    """Convert mono s16le PCM to a device-native int16 channel layout."""

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    samples /= 32768.0
    if target_rate != source_rate:
        divisor = math.gcd(target_rate, source_rate)
        samples = resample_poly(
            samples,
            target_rate // divisor,
            source_rate // divisor,
        ).astype(np.float32, copy=False)
    samples = np.rint(np.clip(samples * 32768.0, -32768, 32767)).astype(np.int16)
    if channels == 1:
        return samples.reshape(-1, 1)
    return np.repeat(samples.reshape(-1, 1), channels, axis=1)


class AudioBuffer:
    """Thread-safe mono float32 FIFO used by the real-time mixer callback."""

    def __init__(self):
        self._chunks: deque[np.ndarray] = deque()
        self._pending_frames = 0
        self._condition = threading.Condition()

    def write(self, samples: np.ndarray):
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size:
            with self._condition:
                self._chunks.append(values.copy())
                self._pending_frames += values.size
                self._condition.notify_all()

    def read(self, frames: int) -> np.ndarray:
        result = np.zeros(frames, dtype=np.float32)
        written = 0
        with self._condition:
            while written < frames and self._chunks:
                current = self._chunks[0]
                count = min(frames - written, current.size)
                result[written : written + count] = current[:count]
                written += count
                self._pending_frames -= count
                if count == current.size:
                    self._chunks.popleft()
                else:
                    self._chunks[0] = current[count:]
            if self._pending_frames == 0:
                self._condition.notify_all()
        return result

    def clear(self):
        with self._condition:
            self._chunks.clear()
            self._pending_frames = 0
            self._condition.notify_all()

    def wait_empty(self, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._pending_frames == 0,
                timeout=timeout,
            )

    @property
    def pending_frames(self) -> int:
        with self._condition:
            return self._pending_frames


class MicrophoneMixer:
    """Continuously mix a physical microphone and generated speech into VB-CABLE."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stream: sd.Stream | None = None
        self._tts = AudioBuffer()
        self._input_device: int | None = None
        self._output_device: int | None = None
        self._sample_rate = 44100
        self._output_channels = 2
        self._mic_gain = 1.0
        self._tts_gain = 1.0
        self._last_error: str | None = None
        self._last_status: str | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stream is not None and bool(self._stream.active)

    @property
    def output_device(self) -> int | None:
        with self._lock:
            return self._output_device

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._stream is not None and bool(self._stream.active),
                "input_device": self._input_device,
                "output_device": self._output_device,
                "sample_rate": self._sample_rate,
                "mic_gain": self._mic_gain,
                "tts_gain": self._tts_gain,
                "last_error": self._last_error,
                "last_status": self._last_status,
            }

    def start(
        self,
        input_device: int,
        output_device: int,
        mic_gain: float = 1.0,
        tts_gain: float = 1.0,
    ) -> dict:
        inputs = list_input_devices()
        outputs = list_output_devices()
        selected_input = next(
            (device for device in inputs if device.id == input_device), None
        )
        if selected_input is None:
            raise ValueError(f"invalid input device id: {input_device}")
        if selected_input.is_vb_cable:
            raise ValueError(
                "CABLE Output cannot be used as the physical microphone (feedback loop)"
            )
        output_device = _stable_cable_device(output_device, outputs)
        selected_output = next(
            (device for device in outputs if device.id == output_device), None
        )
        if selected_output is None:
            raise ValueError(f"invalid output device id: {output_device}")
        if not selected_output.is_vb_cable:
            raise ValueError("the mixer output must be a VB-CABLE input device")

        sample_rate = int(selected_output.default_samplerate)
        output_channels = 2 if selected_output.channels >= 2 else 1
        sd.check_input_settings(
            device=input_device,
            channels=1,
            dtype="float32",
            samplerate=sample_rate,
        )
        sd.check_output_settings(
            device=output_device,
            channels=output_channels,
            dtype="float32",
            samplerate=sample_rate,
        )

        with self._lock:
            already_running = (
                self._stream is not None
                and bool(self._stream.active)
                and self._input_device == input_device
                and self._output_device == output_device
                and self._sample_rate == sample_rate
            )
            if already_running:
                self._mic_gain = float(mic_gain)
                self._tts_gain = float(tts_gain)
                return self.status()

        self.stop()
        with self._lock:
            self._input_device = input_device
            self._output_device = output_device
            self._sample_rate = sample_rate
            self._output_channels = output_channels
            self._mic_gain = float(mic_gain)
            self._tts_gain = float(tts_gain)
            self._last_error = None
            self._last_status = None
            self._tts.clear()
            stream = sd.Stream(
                device=(input_device, output_device),
                samplerate=sample_rate,
                channels=(1, output_channels),
                dtype="float32",
                latency="high",
                callback=self._callback,
            )
            try:
                stream.start()
            except Exception:
                stream.close()
                self._reset_devices()
                raise
            self._stream = stream
        return self.status()

    def inject(self, pcm: bytes, source_rate: int = SAMPLE_RATE):
        with self._lock:
            if self._stream is None or not self._stream.active:
                raise RuntimeError("microphone mixer is not running")
            target_rate = self._sample_rate
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if target_rate != source_rate:
            divisor = math.gcd(target_rate, source_rate)
            samples = resample_poly(
                samples,
                target_rate // divisor,
                source_rate // divisor,
            ).astype(np.float32, copy=False)
        self._tts.write(samples)

    def clear_tts(self):
        self._tts.clear()

    def wait_for_tts(self, timeout: float = 300.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._tts.wait_empty(timeout=min(remaining, 0.25)):
                return True
        return self._tts.pending_frames == 0

    def stop(self):
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except sd.PortAudioError:
                pass
            stream.close()
        self._tts.clear()
        with self._lock:
            self._reset_devices()

    def _reset_devices(self):
        self._input_device = None
        self._output_device = None

    def _callback(self, indata, outdata, frames, time_info, status):
        del time_info
        try:
            if status:
                self._last_status = str(status)
            mic = np.asarray(indata[:, 0], dtype=np.float32)
            tts = self._tts.read(frames)
            mixed = np.clip(mic * self._mic_gain + tts * self._tts_gain, -1.0, 1.0)
            if self._output_channels == 1:
                outdata[:, 0] = mixed
            else:
                outdata[:] = mixed.reshape(-1, 1)
        except Exception as exc:
            self._last_error = str(exc)
            outdata.fill(0)


class AudioRouter:
    """Fan one PCM stream out to VB-CABLE and an optional monitor device."""

    def __init__(
        self,
        output_device: int | None,
        monitor_enabled: bool,
        monitor_device: int | None,
    ):
        devices = list_output_devices()
        output_device = _stable_cable_device(output_device, devices)
        ids: list[int] = []
        if output_device is not None:
            ids.append(output_device)
        if monitor_enabled and monitor_device is not None and monitor_device not in ids:
            ids.append(monitor_device)
        valid_ids = {device.id for device in devices}
        invalid = [device_id for device_id in ids if device_id not in valid_ids]
        if invalid:
            raise ValueError(f"invalid output device id: {invalid[0]}")
        self.sinks: list[_Sink] = []
        self._closed = False
        try:
            for device_id in ids:
                self.sinks.append(_Sink(device_id))
        except Exception:
            self.close(drain=False)
            raise

    def push(self, pcm: bytes):
        for sink in self.sinks:
            sink.put(pcm)

    def abort(self):
        for sink in self.sinks:
            sink.abort()

    def close(self, drain: bool = True):
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        for sink in self.sinks:
            try:
                sink.close(drain=drain)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _stable_cable_device(
    selected_id: int | None, devices: list[AudioDevice]
) -> int | None:
    selected = next((device for device in devices if device.id == selected_id), None)
    if (
        selected is None
        or not selected.is_vb_cable
        or (selected.host_api == "MME" and int(selected.default_samplerate) == 44100)
    ):
        return selected_id
    replacement = next(
        (
            device
            for device in devices
            if device.is_vb_cable
            and device.host_api == "MME"
            and int(device.default_samplerate) == 44100
        ),
        None,
    )
    return replacement.id if replacement is not None else selected_id
