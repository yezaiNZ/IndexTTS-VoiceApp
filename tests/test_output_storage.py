import io
import os
import uuid
import wave
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voice_app.api import create_app
from voice_app.output_store import (
    MARKER,
    GeneratedAudioStore,
    write_wav_atomic,
)
from voice_app.schemas import DesktopSettings, SynthesisRequest


class Tensor:
    """Minimal CPU tensor protocol; no torch/CUDA import or audio devices."""

    def __init__(self, samples):
        self.samples = np.asarray(samples)

    def detach(self):
        return self

    def cpu(self):
        return self

    def clamp(self, low, high):
        return Tensor(self.samples.clip(low, high))

    def to(self, dtype):
        return Tensor(self.samples.astype(np.int16))

    def contiguous(self):
        return self

    def numpy(self):
        return self.samples


class Model:
    def infer(self, **kwargs):
        yield Tensor([0, 100, -100] * 100)
        yield Tensor([1, 200, -200] * 100)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from voice_app import runtime

    class Router:
        def __init__(self, *args):
            pass

        def push(self, pcm):
            pass

        def abort(self):
            pass

        def close(self, drain=True):
            pass

    monkeypatch.setattr(runtime, "AudioRouter", Router)
    monkeypatch.setattr(runtime, "_torch_int16", lambda: None)
    application = create_app(settings_path=tmp_path / "outputs/settings.json")
    application.state.runtime._model = Model()
    return application


def body(tmp_path, **overrides):
    reference = tmp_path / "reference.wav"
    reference.touch()
    return {"text": "测试保存策略", "reference_audio": str(reference), **overrides}


def record(store, pcm=b"\x00\x00" * 50, complete=True):
    recording = store.begin(uuid.uuid4().hex, 22050)
    recording.write(pcm)
    result = recording.finish(complete)
    return recording, result


def test_defaults_disable_save_and_legacy_settings_migrate():
    assert not DesktopSettings().save_output
    assert not DesktopSettings.model_validate({"output_device": 14}).save_output
    assert DesktopSettings().output_limit_mb == 500
    assert not SynthesisRequest(text="hello", reference_audio="sample.wav").save_output


@pytest.mark.parametrize("value", [0, -1, 10241])
def test_retention_limit_is_bounded(value):
    with pytest.raises(ValueError):
        DesktopSettings(output_limit_mb=value)


def test_default_stream_writes_no_audio_or_cache_directory(app, tmp_path):
    with TestClient(app) as client:
        reply = client.post("/v1/tts/stream", json=body(tmp_path))
        assert reply.status_code == 200 and len(reply.content) == 1200
        assert "x-output-file" not in reply.headers
        result = client.get("/v1/outputs/" + reply.headers["x-job-id"]).json()
        assert not result["saved"] and not result["save_requested"]
        assert not client.get("/health").json()["busy"]
    assert not (tmp_path / "outputs").exists()


def test_nonstream_wav_does_not_force_a_disk_file(app, tmp_path):
    with TestClient(app) as client:
        reply = client.post("/v1/tts", json=body(tmp_path))
        assert reply.status_code == 200
        with wave.open(io.BytesIO(reply.content), "rb") as reader:
            assert reader.getnframes() == 600
            assert reader.getframerate() == 22050
        assert "x-output-file" not in reply.headers
    assert not (tmp_path / "outputs").exists()


def test_many_generations_do_not_create_files_or_unbounded_history(app, tmp_path):
    payload = body(tmp_path)
    model = app.state.runtime._model
    with TestClient(app) as client:
        for _ in range(100):
            reply = client.post("/v1/tts/stream", json=payload)
            assert reply.status_code == 200 and len(reply.content) == 1200
    assert not (tmp_path / "outputs").exists()
    assert len(app.state.runtime.output_store._results) == 64
    assert app.state.runtime._model is model


def test_explicit_save_and_persisted_limit(app, tmp_path):
    with TestClient(app) as client:
        assert (
            client.put(
                "/v1/settings", json={"save_output": True, "output_limit_mb": 100}
            ).status_code
            == 200
        )
        assert app.state.runtime.output_store.limit_bytes() == 100 * 1024**2
        reply = client.post("/v1/tts/stream", json=body(tmp_path, save_output=True))
        result = client.get("/v1/outputs/" + reply.headers["x-job-id"]).json()
        assert result["saved"] and result["error"] is None
        path = Path(result["path"])
        assert path.parent == tmp_path / "outputs/auto-saved"
        with wave.open(str(path)) as reader:
            assert reader.getnframes() == 600
        assert not list(path.parent.glob("*.part"))


def test_prunes_oldest_in_own_directory_only(tmp_path):
    root = tmp_path / "outputs/auto-saved"
    store = GeneratedAudioStore(root, lambda: 300)
    oldest, _ = record(store)
    os.utime(oldest.path, (1, 1))
    middle, _ = record(store)
    os.utime(middle.path, (2, 2))
    legacy = root.parent / ("tts-" + uuid.uuid4().hex + ".wav")
    legacy.write_bytes(b"legacy private audio")
    manual = root / "manually-kept.wav"
    manual.write_bytes(b"manual export")
    config = root.parent / "settings.json"
    config.write_text("private settings")
    reference = tmp_path / "presets/my-role/prompt.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"private reference")
    newest, result = record(store)
    assert result["saved"] and result["removed_count"] == 1
    assert not oldest.path.exists() and middle.path.exists() and newest.path.exists()
    assert sum(p.stat().st_size for p in root.glob("tts-*.wav")) <= 300
    assert manual.read_bytes() == b"manual export"
    assert legacy.read_bytes() == b"legacy private audio"
    assert config.read_text() == "private settings"
    assert reference.read_bytes() == b"private reference"


def test_unowned_directory_is_neither_adopted_nor_cleaned(tmp_path):
    root = tmp_path / "foreign"
    root.mkdir()
    foreign = root / ("tts-" + uuid.uuid4().hex + ".wav")
    foreign.write_bytes(b"never delete")
    _, result = record(GeneratedAudioStore(root, lambda: 50))
    assert not result["saved"] and result["error"]
    assert foreign.read_bytes() == b"never delete"
    assert not (root / MARKER).exists()


def test_oversize_and_stopped_recordings_leave_no_partial_file(tmp_path):
    store = GeneratedAudioStore(tmp_path / "cache", lambda: 100)
    recording, result = record(store, b"\x00" * 200)
    assert not result["saved"] and "超过" in result["error"]
    assert not recording.temporary.exists() and not recording.path.exists()
    recording, result = record(store, b"\x00" * 20, complete=False)
    assert not result["saved"]
    assert not recording.temporary.exists() and not recording.path.exists()


def test_locked_old_file_does_not_allow_unbounded_growth(tmp_path, monkeypatch):
    store = GeneratedAudioStore(tmp_path / "cache", lambda: 150)
    old, _ = record(store)
    os.utime(old.path, (1, 1))
    unlink = Path.unlink

    def locked(path, **kwargs):
        if path == old.path:
            raise PermissionError("locked fixture")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    new, result = record(store)
    assert old.path.exists() and not new.path.exists()
    assert not result["saved"] and result["error"]


def test_save_failure_does_not_break_speech_stream(app, tmp_path):
    cache = app.state.runtime.output_store.root
    cache.mkdir(parents=True)
    (cache / "foreign.wav").write_bytes(b"do not touch")
    with TestClient(app) as client:
        reply = client.post("/v1/tts/stream", json=body(tmp_path, save_output=True))
        assert reply.status_code == 200 and len(reply.content) == 1200
        result = client.get("/v1/outputs/" + reply.headers["x-job-id"]).json()
        assert not result["saved"] and result["error"]
        assert not client.get("/health").json()["busy"]


def test_disk_write_failure_does_not_break_stream(app, tmp_path, monkeypatch):
    def no_space(*args):
        raise OSError("fixture no space left")

    monkeypatch.setattr(wave.Wave_write, "writeframesraw", no_space)
    with TestClient(app) as client:
        reply = client.post("/v1/tts/stream", json=body(tmp_path, save_output=True))
        assert reply.status_code == 200 and len(reply.content) == 1200
        result = client.get("/v1/outputs/" + reply.headers["x-job-id"]).json()
        assert not result["saved"] and "no space left" in result["error"]
    assert not list(app.state.runtime.output_store.root.glob("*.part"))


def test_cleanup_old_crash_temporary_only(tmp_path):
    store = GeneratedAudioStore(tmp_path / "cache")
    store._prepare()
    stale = store.root / ("tts-" + uuid.uuid4().hex + ".wav.part")
    recent = store.root / ("tts-" + uuid.uuid4().hex + ".wav.part")
    unrelated = store.root / "private.part"
    for path in (stale, recent, unrelated):
        path.write_bytes(b"fixture")
    os.utime(stale, (1, 1))
    os.utime(unrelated, (1, 1))
    record(store)
    assert not stale.exists() and recent.exists() and unrelated.exists()


def test_generator_failure_discards_partial_and_releases_job(app, tmp_path):
    class BrokenModel:
        def infer(self, **kwargs):
            yield Tensor([1, 2, 3])
            raise RuntimeError("fixture inference failure")

    runtime = app.state.runtime
    runtime._model = BrokenModel()
    session = runtime.start_session(
        SynthesisRequest(**body(tmp_path, save_output=True))
    )
    with pytest.raises(RuntimeError, match="fixture inference failure"):
        list(session.iter_pcm())
    assert not runtime.status()["busy"]
    assert not session.output_path.exists()
    assert not list(runtime.output_store.root.glob("*.part"))


def test_stopping_runtime_discards_unfinished_saved_audio(app, tmp_path):
    runtime = app.state.runtime
    session = runtime.start_session(
        SynthesisRequest(**body(tmp_path, save_output=True))
    )
    stream = session.iter_pcm()
    assert next(stream)
    session.request_stop()
    assert list(stream) == []
    assert not runtime.status()["busy"] and not session.output_path.exists()
    result = runtime.output_store.result(session.job_id)
    assert not result["saved"] and result["error"]
    assert not list(runtime.output_store.root.glob("*.part"))


def test_nonstream_memory_limit_cancels_and_releases_runtime(
    app, tmp_path, monkeypatch
):
    monkeypatch.setattr("voice_app.api.LAST_AUDIO_LIMIT", 100)
    with TestClient(app) as client:
        reply = client.post("/v1/tts", json=body(tmp_path, save_output=True))
        assert reply.status_code == 413
        assert not client.get("/health").json()["busy"]
    assert not list(app.state.runtime.output_store.root.glob("*.wav"))
    assert not list(app.state.runtime.output_store.root.glob("*.part"))


def test_symlink_candidate_is_never_removed(tmp_path):
    store = GeneratedAudioStore(tmp_path / "cache", lambda: 150)
    store._prepare()
    target = tmp_path / "reference.wav"
    target.write_bytes(b"reference")
    link = store.root / ("tts-" + uuid.uuid4().hex + ".wav")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    record(store)
    record(store)
    assert target.read_bytes() == b"reference" and link.is_symlink()


def test_result_history_does_not_accumulate_forever(tmp_path):
    store = GeneratedAudioStore(tmp_path / "cache")
    for n in range(100):
        store.remember(str(n), {"saved": False})
    assert len(store._results) == 64 and store.result("0") is None
    assert not store.root.exists()


def test_manual_export_is_valid_wav_and_atomic(tmp_path, monkeypatch):
    destination = tmp_path / "keep-this.wav"
    write_wav_atomic(destination, b"\x00\x00" * 30)
    original = destination.read_bytes()
    with wave.open(str(destination)) as reader:
        assert reader.getnframes() == 30 and reader.getframerate() == 22050

    def fail(*args):
        raise OSError("fixture disk error")

    monkeypatch.setattr("voice_app.output_store.os.replace", fail)
    with pytest.raises(OSError):
        write_wav_atomic(destination, b"\x01\x00" * 50)
    assert destination.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_manual_export_cannot_use_automatic_cache_filename(tmp_path):
    with pytest.raises(ValueError, match="自动缓存"):
        write_wav_atomic(tmp_path / ("tts-" + uuid.uuid4().hex + ".wav"), b"\x00\x00")


@pytest.fixture
def window(monkeypatch):
    from voice_app.desktop_qt import VoiceDesktopQt

    monkeypatch.setattr(VoiceDesktopQt, "_refresh_data", lambda self: None)
    widget = VoiceDesktopQt("http://127.0.0.1:7862", model_ready=True)
    widget.text.setPlainText("测试")
    widget.reference.setEditText("reference.wav")
    widget.output_device.addItem("CABLE", 14)
    widget.output_device.setCurrentIndex(0)
    yield widget
    widget.generating = False
    widget.close()


def test_desktop_defaults_no_save_and_keeps_latest_for_export(window):
    assert not window._payload()["save_output"]
    assert not window.save_last_button.isEnabled()
    pcm = b"\x00\x00" * 100
    window._generation_done({"pcm": pcm, "too_large": False, "saved": None})
    assert window._last_pcm == pcm and window.save_last_button.isEnabled()
    assert "未自动保存" in window.status_pill.text()
    assert "另存上一条" in window.status_detail.text()
    window.save_output.setChecked(True)
    assert window._payload()["save_output"] and window.output_limit.isEnabled()


def test_desktop_automatic_save_uses_result_not_unconfirmed_header(window):
    window._generation_done(
        {"pcm": b"\x00\x00", "saved": {"saved": False, "error": "磁盘空间不足"}}
    )
    assert "未自动保存" in window.status_pill.text()
    assert "磁盘空间不足" in window.status_detail.text()
    assert window.save_last_button.isEnabled()


@pytest.mark.parametrize("save_output", [False, True])
def test_playback_failure_keeps_generated_audio_and_is_reported(
    app, tmp_path, monkeypatch, save_output
):
    from voice_app import runtime
    from voice_app.audio import PlaybackError

    router = runtime.AudioRouter

    class BrokenHeadphones(router):
        def close(self, drain=True):
            if drain:
                raise PlaybackError("耳机播放等待超时")

    monkeypatch.setattr(runtime, "AudioRouter", BrokenHeadphones)
    with TestClient(app) as client:
        reply = client.post("/v1/tts/stream", json=body(tmp_path, save_output=save_output))
        assert reply.status_code == 200 and len(reply.content) == 1200
        result = client.get("/v1/outputs/" + reply.headers["x-job-id"]).json()
        assert result["playback_error"] == "耳机播放等待超时"
        assert result["saved"] is save_output
        assert not client.get("/health").json()["busy"]
        if save_output:
            with wave.open(result["path"], "rb") as reader:
                assert reader.readframes(reader.getnframes()) == reply.content


def test_desktop_reports_playback_failure_even_without_auto_save(window, monkeypatch):
    from voice_app import desktop_qt

    pcm = b"\xe8\x03" * 20

    class Reply:
        headers: ClassVar[dict] = {"X-Job-Id": "test-job"}
        ok = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, **kwargs):
            yield pcm

    def result(path):
        assert path == "/v1/outputs/test-job"
        return {"saved": False, "playback_error": "Arctis Nova 3：等待设备播完超时。"}

    monkeypatch.setattr(desktop_qt.requests, "post", lambda *a, **k: Reply())
    monkeypatch.setattr(window, "_put", lambda *args: None)
    monkeypatch.setattr(window, "_configure_mixer", lambda *args: None)
    monkeypatch.setattr(window, "_get", result)
    monkeypatch.setattr(window, "_run_async", lambda task, done, error: done(task()))
    assert not window.save_output.isChecked()
    window.generate()
    assert "生成完成 · 播放异常" in window.status_pill.text()
    assert "Arctis Nova 3" in window.status_detail.text()
    assert window.save_last_button.isEnabled() and window._last_pcm == pcm


def test_desktop_manual_export_does_not_change_auto_save(window, tmp_path, monkeypatch):
    from voice_app.desktop_qt import QFileDialog

    window._last_pcm = b"\x00\x00" * 20
    destination = tmp_path / "manual.wav"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *args: (str(destination), "")
    )
    window.save_last_audio()
    assert destination.is_file() and not window.save_output.isChecked()
    assert "不参与自动清理" in window.status_detail.text()


def test_desktop_buffer_is_bounded_and_discards_previous(window, monkeypatch):
    from voice_app import desktop_qt

    class Reply:
        headers: ClassVar[dict] = {"X-Job-Id": uuid.uuid4().hex}
        status_code = 200
        ok = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, **kwargs):
            yield b"\x00" * 20
            yield b"\x00" * 20

    monkeypatch.setattr(desktop_qt, "LAST_AUDIO_LIMIT", 32)
    monkeypatch.setattr(desktop_qt.requests, "post", lambda *a, **k: Reply())
    monkeypatch.setattr(window, "_put", lambda *args: None)
    monkeypatch.setattr(window, "_configure_mixer", lambda *args: None)
    monkeypatch.setattr(window, "_run_async", lambda task, done, error: done(task()))
    window._last_pcm = b"old audio"
    window.generate()
    assert window._last_pcm is None and not window.save_last_button.isEnabled()
    assert "64 MB" in window.status_detail.text()
    assert not window.generating


def test_storage_controls_do_not_overlap_at_small_window(window):
    from PySide6.QtWidgets import QApplication

    window.show()
    window.resize(980, 760)
    QApplication.processEvents()
    assert window.workspace_scroll.verticalScrollBar().isVisible()
    assert window.text.geometry().bottom() < window.save_output.parent().height()


def test_remember_storage_preferences_preserves_other_settings(window, monkeypatch):
    saved = []
    monkeypatch.setattr(
        window, "_get", lambda path: {"microphone_device": 2, "output_device": 14}
    )
    monkeypatch.setattr(window, "_put", lambda path, settings: saved.append(settings))
    monkeypatch.setattr(window, "_run_async", lambda task, done, error: done(task()))
    window.save_output.setChecked(True)
    window._select_combo(window.output_limit, 1024)
    window.save_storage_preferences()
    assert saved == [
        {
            "microphone_device": 2,
            "output_device": 14,
            "save_output": True,
            "output_limit_mb": 1024,
        }
    ]
