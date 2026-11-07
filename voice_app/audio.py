from __future__ import annotations

import logging
import math
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from .schemas import AudioDevice
from .windows_audio import is_device_mapper, preferred_wave_name

SAMPLE_RATE = 22050
_PLAYBACK_GRACE_SECONDS = 5.0
_LOG = logging.getLogger(__name__)


def list_output_devices() -> list[AudioDevice]:
    return _list_devices(input_device=False)


def list_input_devices() -> list[AudioDevice]:
    return _list_devices(input_device=True)


def _list_devices(*, input_device: bool) -> list[AudioDevice]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    channel_key = "max_input_channels" if input_device else "max_output_channels"
    cable_name = "cable output" if input_device else "cable input"
    result: list[AudioDevice] = []
    for index, device in enumerate(devices):
        channels = int(device.get(channel_key, 0))
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
                is_vb_cable=(cable_name in normalized or "vb-audio" in normalized),
            )
        )
    default = _default_device_index(result, input_device=input_device)
    for device in result:
        device.is_default = device.id == default
    return result


def _default_device_index(devices: list[AudioDevice], *, input_device: bool) -> int | None:
    if sys.platform == "win32":
        # PortAudio's MME default often points at the opaque Sound Mapper.
        # Resolve the real endpoint each time and don't guess when names collide.
        preferred = preferred_wave_name(input_device=input_device)
        if not preferred:
            return None
        matches = [
            device.id for device in devices
            if device.host_api == "MME"
            and device.name.strip().casefold() == preferred.strip().casefold()
        ]
        return matches[0] if len(matches) == 1 else None
    try:
        value = sd.default.device[0 if input_device else 1]
        return int(value) if value is not None and int(value) >= 0 else None
    except (TypeError, ValueError, sd.PortAudioError):
        return None


def resolve_system_device(devices: list[AudioDevice], *, input_device: bool) -> int:
    name = "麦克风" if input_device else "耳机或扬声器"
    selected = next((device for device in devices if device.is_default), None)
    if selected is None or is_device_mapper(selected.name):
        raise ValueError(f"找不到可用的系统默认{name}，请检查 Windows 声音设置或手动选择设备。")
    if selected.is_vb_cable:
        raise ValueError(f"系统默认设备是 VB-CABLE，请手动选择用于本地使用的{name}。")
    return selected.id


class PlaybackError(RuntimeError):
    """Generated audio could not be delivered to an output device."""


@dataclass
class _Sink:
    device_id: int
    source_rate: int = SAMPLE_RATE

    def __post_init__(self):
        info = sd.query_devices(self.device_id)
        self.target_rate = int(float(info["default_samplerate"]))
        self.channels = 2 if int(info["max_output_channels"]) >= 2 else 1
        self.name = str(info.get("name", self.device_id))
        self.items: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.error: Exception | None = None
        self.stream = None
        self._stream_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending_bytes = 0
        self.worker = threading.Thread(
            target=self._run,
            name=f"audio-device-{self.device_id}",
            daemon=True,
        )
        self.worker.start()

    def put(self, pcm: bytes):
        if not pcm:
            return
        with self._state_lock:
            self._pending_bytes += len(pcm)
        queued = False
        try:
            queued = self._enqueue(pcm, self._drain_deadline())
        finally:
            if not queued:
                self._consume(len(pcm))

    def _consume(self, size: int):
        with self._state_lock:
            self._pending_bytes -= size

    def _drain_deadline(self) -> float:
        with self._state_lock:
            remaining = self._pending_bytes / (2 * self.source_rate)
        return time.monotonic() + remaining + _PLAYBACK_GRACE_SECONDS

    def _failure(self, detail: str) -> PlaybackError:
        return PlaybackError(f"{self.name}（设备 {self.device_id}）：{detail}")

    def _raise_worker_error(self):
        if self.error is not None:
            raise self._failure(f"播放失败：{self.error}") from self.error

    def _enqueue(self, item: bytes | None, deadline: float) -> bool:
        while not self.stopping.is_set():
            self._raise_worker_error()
            if self.finished.is_set():
                if item is None:
                    return False
                raise self._failure("播放设备提前关闭。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.abort()
                raise self._failure("等待播放设备接收音频超时。")
            try:
                self.items.put(item, timeout=min(remaining, 0.05))
                return True
            except queue.Full:
                continue
        return False

    def _run(self):
        stream = None
        underflows = 0
        try:
            while not self.stopping.is_set():
                item = self.items.get()
                if item is None:
                    break
                try:
                    samples = convert_pcm(
                        item,
                        source_rate=self.source_rate,
                        target_rate=self.target_rate,
                        channels=self.channels,
                    )
                    if self.stopping.is_set():
                        break
                    if stream is None:
                        # Prepare the first samples before opening the device.
                        # Don't run an empty playback stream during model inference.
                        stream = sd.OutputStream(
                            device=self.device_id,
                            samplerate=self.target_rate,
                            channels=self.channels,
                            dtype="int16",
                            latency="high",
                        )
                        with self._stream_lock:
                            self.stream = stream
                        if self.stopping.is_set():
                            break
                        stream.start()
                    if not self.stopping.is_set():
                        underflows += bool(stream.write(samples))
                finally:
                    self._consume(len(item))
            if stream is not None and not self.stopping.is_set():
                # PortAudio stop drains the device; a failure is not a success.
                stream.stop()
        except Exception as exc:  # noqa: BLE001 - relay driver/thread failures to the caller
            if not self.stopping.is_set():
                self.error = exc
        finally:
            try:
                with self._stream_lock:
                    self.stream = None
                if stream is not None:
                    stream.close()
            except Exception as exc:  # noqa: BLE001 - a failed device close is also a playback failure
                if not self.stopping.is_set():
                    self.error = self.error or exc
            finally:
                self.finished.set()
            if underflows:
                _LOG.warning(
                    "Audio output device %d (%s) reported %d buffer underflow(s)",
                    self.device_id, self.name, underflows,
                )

    def close(self, drain: bool = True):
        deadline = self._drain_deadline() if drain else time.monotonic() + _PLAYBACK_GRACE_SECONDS
        try:
            if drain:
                if not self.finished.is_set():
                    self._enqueue(None, deadline)
            else:
                self.abort()
            if not self.finished.wait(max(0.0, deadline - time.monotonic())):
                raise self._failure("等待设备播完超时，音频可能未完整播放。")
            if drain:
                self._raise_worker_error()
        except PlaybackError:
            self.abort()
            raise

    def abort(self):
        with self._state_lock:
            if self.stopping.is_set():
                return
            self.stopping.set()
        try:
            self.items.put_nowait(None)
        except queue.Full:
            pass  # The worker checks stopping before it takes another item.
        if self.stream is not None:
            # A faulty driver must not block the API's stop/timeout response.
            threading.Thread(target=self._abort_device, daemon=True).start()

    def _abort_device(self):
        with self._stream_lock:
            if self.stream is not None:
                try:
                    self.stream.abort()
                except sd.PortAudioError:
                    pass


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
        input_device: int | None,
        output_device: int,
        mic_gain: float = 1.0,
        tts_gain: float = 1.0,
    ) -> dict:
        inputs = list_input_devices()
        outputs = list_output_devices()
        if input_device is None:
            input_device = resolve_system_device(inputs, input_device=True)
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
        if monitor_enabled:
            if monitor_device is None:
                monitor_device = resolve_system_device(devices, input_device=False)
            if monitor_device not in ids:
                ids.append(monitor_device)
        valid_ids = {device.id for device in devices}
        invalid = [device_id for device_id in ids if device_id not in valid_ids]
        if invalid:
            raise ValueError(f"invalid output device id: {invalid[0]}")
        self.sinks: list[_Sink] = []
        self._failed_sinks: dict[int, PlaybackError] = {}
        self._closed = False
        try:
            for device_id in ids:
                self.sinks.append(_Sink(device_id))
        except Exception:
            self.close(drain=False)
            raise

    def push(self, pcm: bytes):
        for sink in self.sinks:
            if sink.device_id not in self._failed_sinks:
                try:
                    sink.put(pcm)
                except PlaybackError as exc:
                    self._failed_sinks[sink.device_id] = exc
                    sink.abort()

    def abort(self):
        for sink in self.sinks:
            sink.abort()

    def close(self, drain: bool = True):
        if self._closed:
            return
        self._closed = True
        errors = list(self._failed_sinks.values()) if drain else []
        for sink in self.sinks:
            try:
                sink.close(drain=drain and sink.device_id not in self._failed_sinks)
            except PlaybackError as exc:
                errors.append(exc)
        if errors:
            raise PlaybackError("；".join(str(error) for error in errors))


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
