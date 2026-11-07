import threading

import pytest

from voice_app import audio
from voice_app.schemas import AudioDevice

PCM = b"\xe8\x03" * 200


@pytest.fixture
def stream_factory(monkeypatch):
    events = []
    monkeypatch.setattr(
        audio.sd,
        "query_devices",
        lambda index: {
            "name": "Headphones",
            "default_samplerate": 1000,
            "max_output_channels": 2,
        },
    )

    class Stream:
        def __init__(self, **kwargs):
            events.append("open")

        def start(self):
            events.append("start")

        def write(self, samples):
            events.append(("write", samples.shape))
            return False

        def stop(self):
            events.append("drain")

        def abort(self):
            events.append("abort")

        def close(self):
            events.append("close")

    monkeypatch.setattr(audio.sd, "OutputStream", Stream)
    return Stream, events


def test_stream_opens_only_after_audio_is_ready_and_drains(stream_factory):
    _, events = stream_factory
    sink = audio._Sink(15, source_rate=1000)
    assert events == []  # The model can be warming up; no empty stream is running.
    sink.put(PCM)
    sink.close()
    assert events == ["open", "start", ("write", (200, 2)), "drain", "close"]


def test_stopping_before_audio_never_opens_device(stream_factory):
    _, events = stream_factory
    sink = audio._Sink(15, source_rate=1000)
    sink.abort()
    sink.put(PCM)
    sink.close(drain=False)
    assert events == []


@pytest.mark.parametrize("stage", ["start", "write", "stop"])
def test_driver_error_is_not_silent_success(monkeypatch, stream_factory, stage):
    stream, events = stream_factory

    def fail(*args):
        raise audio.sd.PortAudioError(f"{stage} failure")

    monkeypatch.setattr(stream, stage, fail)
    sink = audio._Sink(15, source_rate=1000)
    sink.put(PCM)
    with pytest.raises(audio.PlaybackError, match=f"{stage} failure"):
        sink.close()
    assert sink.finished.wait(1) and "close" in events


def test_drain_wait_accounts_for_remaining_audio_length(monkeypatch, stream_factory):
    stream, events = stream_factory
    monkeypatch.setattr(audio, "_PLAYBACK_GRACE_SECONDS", 0.025)
    write = stream.write

    def slow_write(self, samples):
        threading.Event().wait(0.1)
        return write(self, samples)

    monkeypatch.setattr(stream, "write", slow_write)
    sink = audio._Sink(15, source_rate=1000)
    sink.put(PCM)
    sink.close()
    assert "drain" in events and "abort" not in events


def test_stalled_device_reports_timeout_and_aborts(monkeypatch, stream_factory):
    stream, events = stream_factory
    monkeypatch.setattr(audio, "_PLAYBACK_GRACE_SECONDS", 0.05)
    entered, released = threading.Event(), threading.Event()

    def stall(self, samples):
        entered.set()
        released.wait(2)

    def abort(self):
        events.append("abort")
        released.set()

    monkeypatch.setattr(stream, "write", stall)
    monkeypatch.setattr(stream, "abort", abort)
    sink = audio._Sink(15, source_rate=1000)
    sink.put(PCM[:2])
    try:
        assert entered.wait(1)
        with pytest.raises(audio.PlaybackError, match="超时"):
            sink.close()
        assert sink.finished.wait(1)
        assert "abort" in events and "close" in events
    finally:
        released.set()


def test_headphone_failure_does_not_cancel_the_other_output(monkeypatch):
    calls = []
    monkeypatch.setattr(
        audio,
        "list_output_devices",
        lambda: [
            AudioDevice(
                id=index,
                name=name,
                host_api="MME",
                channels=2,
                default_samplerate=44100,
                is_vb_cable=index == 14,
            )
            for index, name in ((14, "CABLE Input"), (15, "Headphones"))
        ],
    )

    class Sink:
        def __init__(self, index):
            self.device_id = index

        def put(self, pcm):
            if self.device_id == 15:
                raise audio.PlaybackError("Headphones unavailable")
            calls.append((self.device_id, pcm))

        def abort(self):
            pass

        def close(self, drain=True):
            calls.append((self.device_id, drain))

    monkeypatch.setattr(audio, "_Sink", Sink)
    router = audio.AudioRouter(14, True, 15)
    router.push(PCM)
    router.push(PCM)
    with pytest.raises(audio.PlaybackError, match="Headphones unavailable"):
        router.close()
    assert calls == [(14, PCM), (14, PCM), (14, True), (15, False)]
