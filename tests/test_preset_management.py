from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voice_app.api import create_app
from voice_app.presets import PresetResolver, PresetStore, preset_summaries
from voice_app.reference_audio import ReferencePreview, prepare_reference
from voice_app.runtime import RuntimeOptions
from voice_app.schemas import AudioDevice, SynthesisRequest


def sample(seconds=1.2, rate=16000, channels=1, format="WAV", silent=False):
    values = (
        np.zeros(int(seconds * rate))
        if silent
        else 0.2 * np.sin(np.arange(int(seconds * rate)) * 2 * np.pi * 240 / rate)
    )
    values = np.repeat(values[:, None], channels, axis=1)
    output = io.BytesIO()
    sf.write(output, values, rate, format=format)
    return output.getvalue()


@pytest.mark.parametrize(
    "format,suffix", [("WAV", "wav"), ("FLAC", "flac"), ("OGG", "ogg"), ("MP3", "mp3")]
)
def test_import_decodes_supported_formats_and_standardizes(format, suffix):
    result = prepare_reference(sample(format=format), "参考." + suffix)
    with wave.open(io.BytesIO(result.wav)) as reader:
        assert reader.getnchannels() == 1
        assert reader.getframerate() == 22050
        assert reader.getsampwidth() == 2
        assert 1 <= reader.getnframes() / 22050 <= 1.3
    assert result.pcm and 1 <= result.seconds <= 1.3


@pytest.mark.parametrize(
    "content,filename,reason",
    [
        (b"", "empty.wav", "非空"),
        (b"not audio", "fake.wav", "无法解码"),
        (b"not audio", "voice.m4a", "支持 WAV"),
    ],
)
def test_invalid_audio_has_actionable_error(content, filename, reason):
    with pytest.raises(ValueError, match=reason):
        prepare_reference(content, filename)


@pytest.mark.parametrize(
    "seconds,shown", [(0.1, "0.10"), (0.999, "0.999")]
)
def test_short_audio_error_reports_detected_length_and_action(seconds, shown):
    with pytest.raises(ValueError, match="不足 1 秒") as error:
        prepare_reference(sample(seconds), "voice.wav")
    message = str(error.value)
    assert f"为 {shown} 秒" in message
    assert "请选择更长的片段" in message


@pytest.mark.parametrize("seconds", [1, 30, 30.04, 65])
def test_audio_normalization_preserves_full_duration(seconds):
    reference = prepare_reference(sample(seconds), "voice.wav")
    assert reference.seconds == pytest.approx(seconds, abs=1 / 16000 + 1 / 22050)
    with wave.open(io.BytesIO(reference.wav)) as reader:
        assert reader.getnframes() / reader.getframerate() == reference.seconds


def test_compressed_audio_checks_decoded_and_normalized_size(monkeypatch):
    content = sample(4, rate=8000, format="FLAC")
    with monkeypatch.context() as patches:
        patches.setattr("voice_app.reference_audio.MAX_DECODED_BYTES", 64_000)
        with pytest.raises(ValueError, match="解码后的数据量过大"):
            prepare_reference(content, "voice.flac")
    # Compressed input fits, but its expanded PCM WAV would exceed the file budget.
    monkeypatch.setattr("voice_app.reference_audio.MAX_UPLOAD_BYTES", len(content))
    with pytest.raises(ValueError, match="转换后的 WAV"):
        prepare_reference(content, "voice.flac")


def test_reject_silence_multichannel_and_oversize(monkeypatch):
    with pytest.raises(ValueError, match="无声"):
        prepare_reference(sample(silent=True), "voice.wav")
    with pytest.raises(ValueError, match="单声道或双声道"):
        prepare_reference(sample(channels=4), "voice.wav")
    monkeypatch.setattr("voice_app.reference_audio.MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError, match="25 MB"):
        prepare_reference(b"x" * 11, "voice.wav")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        ".hidden",
        "../other",
        "C:\\elsewhere",
        "a/b",
        "a?b",
        "NUL",
        "CON.txt",
        "LPT1",
        "a.",
        "a\x00",
        "x" * 61,
    ],
)
def test_unsafe_or_windows_reserved_role_names_rejected(name):
    with pytest.raises(ValueError, match="角色名"):
        PresetStore.valid_name(name)


def test_new_role_owns_audio_and_survives_original_removal(tmp_path):
    original = tmp_path / "original.wav"
    original.write_bytes(sample())
    store = PresetStore(tmp_path / "presets")
    result = store.create(
        "自己的角色", prepare_reference(original.read_bytes(), original.name), "JA"
    )
    original.unlink()
    assert result["reference_exists"] and Path(result["reference_audio"]).is_file()
    data = json.loads((store.root / "自己的角色/preset.json").read_text("utf-8"))
    assert data["prompt_audio"] == "prompt.wav" and data["language"] == "JA"
    resolved = PresetResolver(store).resolve(
        SynthesisRequest(text="测试", preset="自己的角色")
    )
    assert resolved.reference_audio == result["reference_audio"]


def test_duplicate_does_not_overwrite_and_hidden_staging_not_listed(tmp_path):
    store = PresetStore(tmp_path / "presets")
    audio = prepare_reference(sample(), "voice.wav")
    first = store.create("Role", audio)
    before = Path(first["reference_audio"]).read_bytes()
    with pytest.raises(FileExistsError):
        store.create("role", audio)
    assert Path(first["reference_audio"]).read_bytes() == before
    hidden = store.root / ".import-incomplete"
    hidden.mkdir()
    (hidden / "preset.json").write_text("{}")
    assert store.list_names() == ["Role"]


def test_rename_keeps_legacy_audio_emotion_and_custom_parameters(tmp_path):
    store = PresetStore(tmp_path / "presets")
    store.create("旧角色", prepare_reference(sample(), "voice.wav"))
    config = store.root / "旧角色/preset.json"
    data = json.loads(config.read_text("utf-8"))
    data.update(advanced_params={"top_p": 0.6}, emo_audio="prompt.wav", emo_weight=0.42)
    config.write_text(json.dumps(data), encoding="utf-8")
    result = store.update("旧角色", "新角色", language="EN")
    loaded = store.load("新角色")
    assert result["reference_exists"] and store.load("旧角色") is None
    assert Path(loaded["emo_audio"]).is_file()
    assert loaded["advanced_params"] == {"top_p": 0.6} and loaded["emo_weight"] == 0.42


def test_failed_update_rolls_back_name_and_new_audio(tmp_path, monkeypatch):
    store = PresetStore(tmp_path / "presets")
    audio = prepare_reference(sample(), "voice.wav")
    store.create("old", audio)
    before = (store.root / "old/preset.json").read_bytes()

    def fail(*args):
        raise OSError("fixture disk error")

    monkeypatch.setattr("voice_app.presets.os.replace", fail)
    with pytest.raises(OSError):
        store.update("old", "new", audio)
    assert (store.root / "old/preset.json").read_bytes() == before
    assert not (store.root / "new").exists()
    assert not list((store.root / "old").glob("reference-*"))


def test_replacement_keeps_previous_audio_and_soft_delete_is_recoverable(tmp_path):
    original = tmp_path / "original.wav"
    original.write_bytes(sample())
    store = PresetStore(tmp_path / "presets")
    audio = prepare_reference(original.read_bytes(), original.name)
    store.create("role", audio)
    before = (store.root / "role/prompt.wav").read_bytes()
    result = store.update(
        "role", "role", prepare_reference(sample(rate=44100), "new.wav")
    )
    assert Path(result["reference_audio"]).name.startswith("reference-")
    assert (store.root / "role/prompt.wav").read_bytes() == before
    destination = store.trash("role")
    assert (
        not store.list_names() and (destination / "prompt.wav").read_bytes() == before
    )
    assert original.is_file()


def test_malformed_legacy_preset_can_be_listed_for_repair(tmp_path):
    store = PresetStore(tmp_path / "presets")
    folder = store.root / "broken"
    folder.mkdir(parents=True)
    (folder / "preset.json").write_text('{"prompt_audio": {}, "language": {}}')
    assert not preset_summaries(store)[0].reference_exists


def test_broken_json_role_can_be_repaired_by_importing_new_audio(tmp_path):
    store = PresetStore(tmp_path / "presets")
    folder = store.root / "broken"
    folder.mkdir(parents=True)
    (folder / "preset.json").write_text("broken json")
    with pytest.raises(ValueError, match="修复预设"):
        store.update("broken", "repaired")
    assert store.update("broken", "repaired", prepare_reference(sample(), "voice.wav"))[
        "reference_exists"
    ]


def test_renaming_legacy_absolute_audio_paths_does_not_break_reference(tmp_path):
    store = PresetStore(tmp_path / "presets")
    result = store.create("old", prepare_reference(sample(), "voice.wav"))
    path = store.root / "old/preset.json"
    data = json.loads(path.read_text("utf-8"))
    data["prompt_audio"] = result["reference_audio"]
    data["emo_audio"] = result["reference_audio"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.update("old", "new")["reference_exists"]
    assert Path(store.load("new")["emo_audio"]).is_file()


@pytest.fixture
def client(tmp_path):
    app = create_app(
        RuntimeOptions(presets_dir=tmp_path / "presets"),
        tmp_path / "outputs/settings.json",
    )
    with TestClient(app) as value:
        yield value


def upload(client, name="角色", content=None):
    return client.post(
        "/v1/presets",
        params={"name": name, "filename": "voice.wav"},
        content=sample() if content is None else content,
        headers={"Content-Type": "application/octet-stream"},
    )


def test_api_role_lifecycle_without_model_or_audio_device(client):
    assert upload(client).status_code == 201
    assert not client.get("/health").json()["model_loaded"]
    settings = client.get("/v1/settings").json()
    settings["last_preset"] = "角色"
    client.put("/v1/settings", json=settings)
    renamed = client.put(
        "/v1/presets/角色",
        params={"new_name": "新名字", "language": "EN"},
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert renamed.status_code == 200 and renamed.json()["reference_exists"]
    assert client.get("/v1/settings").json()["last_preset"] == "新名字"
    assert (
        client.post("/v1/presets/新名字/trash", json={"confirm": False}).status_code
        == 422
    )
    removed = client.post("/v1/presets/新名字/trash", json={"confirm": True})
    assert removed.status_code == 200 and removed.json()["recoverable"]
    assert Path(removed.json()["trash_path"]).is_dir()
    assert client.get("/v1/presets").json() == []
    assert client.get("/v1/settings").json()["last_preset"] == ""


def test_api_rejects_bad_audio_duplicate_cross_origin_and_oversize(client, monkeypatch):
    assert upload(client, content=b"invalid").status_code == 422
    assert upload(client).status_code == 201
    assert upload(client).status_code == 409
    assert (
        client.post(
            "/v1/presets/角色/trash",
            json={"confirm": True},
            headers={"Origin": "https://untrusted.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/presets",
            params={"name": "bad", "filename": "voice.wav"},
            content=sample(),
            headers={"Content-Type": "text/plain"},
        ).status_code
        == 415
    )
    monkeypatch.setattr("voice_app.api.MAX_UPLOAD_BYTES", 10)
    assert upload(client, "oversize", content=b"x" * 11).status_code == 413


def test_api_invalid_replace_preserves_existing_and_busy_blocks_edit(client):
    upload(client)
    store = client.app.state.runtime.resolver.store
    original = store.reference_bytes("角色")
    response = client.put(
        "/v1/presets/角色",
        params={"new_name": "new", "filename": "bad.wav"},
        content=b"not audio",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 422 and store.reference_bytes("角色") == original
    runtime = client.app.state.runtime
    runtime._generation_lock.acquire()
    try:
        assert (
            client.post("/v1/presets/角色/trash", json={"confirm": True}).status_code
            == 409
        )
    finally:
        runtime._generation_lock.release()
    assert store.reference_bytes("角色") == original


@pytest.mark.parametrize("seconds", [1.2, 65])
def test_preview_endpoint_plays_full_saved_reference_without_model(
    client, monkeypatch, seconds
):
    imported = upload(client, content=sample(seconds))
    assert imported.status_code == 201
    assert imported.json()["duration_seconds"] == seconds
    calls = []
    monkeypatch.setattr(
        client.app.state.preview,
        "play",
        lambda reference, device: calls.append((reference, device)),
    )
    response = client.post(
        "/v1/preset-preview",
        params={"preset": "角色", "device_id": 16},
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200 and calls[0][1] == 16 and calls[0][0].pcm
    assert response.json()["duration_seconds"] == calls[0][0].seconds == seconds
    assert not client.get("/health").json()["model_loaded"]
    assert client.post("/v1/preset-preview/stop").status_code == 200


def device(id, name, cable=False):
    return AudioDevice(
        id=id,
        name=name,
        host_api="MME",
        channels=2,
        default_samplerate=44100,
        is_vb_cable=cable,
    )


def test_preview_cannot_send_reference_into_vb_or_default_mapper(monkeypatch):
    monkeypatch.setattr(
        "voice_app.audio.list_output_devices",
        lambda: [
            device(14, "CABLE Input", True),
            device(9, "Microsoft 声音映射器 - Output"),
        ],
    )
    for id in (9, 14, 99):
        with pytest.raises(ValueError, match="耳机"):
            ReferencePreview().play(prepare_reference(sample(), "voice.wav"), id)


@pytest.mark.parametrize("selected", [16, None])
def test_preview_routes_only_to_headphones_and_can_stop_without_mic(monkeypatch, selected):
    preview = ReferencePreview()
    calls = []

    class Router:
        def __init__(self, *args):
            calls.append(args)

        def push(self, pcm):
            calls.append(len(pcm))
            preview.stop()

        def abort(self):
            calls.append("abort")

        def close(self, drain):
            calls.append(("close", drain))

    monkeypatch.setattr(
        "voice_app.audio.list_output_devices",
        lambda: [device(16, "Headphones").model_copy(update={"is_default": True})],
    )
    monkeypatch.setattr("voice_app.audio.AudioRouter", Router)
    preview.play(prepare_reference(sample(), "voice.wav"), selected)
    assert calls[0] == (None, True, 16) and calls[-1] == ("close", False)


@pytest.fixture
def window(monkeypatch):
    from voice_app.desktop_qt import VoiceDesktopQt

    monkeypatch.setattr(VoiceDesktopQt, "_refresh_data", lambda self: None)
    widget = VoiceDesktopQt("http://127.0.0.1:1", model_ready=True)
    widget.text.setPlainText("测试")
    widget.monitor_enabled.setChecked(True)
    widget.monitor_device.addItem("Headphones", 16)
    widget.monitor_device.setCurrentIndex(0)
    yield widget
    widget.close()


def test_first_run_has_discoverable_import_and_missing_role_guidance(window):
    assert "导入角色音频" in window.import_preset_button.text()
    assert not window.manage_preset_button.isEnabled()
    with pytest.raises(ValueError, match="导入角色音频"):
        window._payload()


def test_import_is_visible_without_scrolling_at_minimum_window_size(window):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    window._set_presets([])
    window.show()
    window.resize(980, 760)
    QApplication.processEvents()
    viewport = window.workspace_scroll.viewport()
    position = window.import_preset_button.mapTo(viewport, QPoint(0, 0))
    assert position.y() >= 0
    assert position.y() + window.import_preset_button.height() < viewport.height()
    assert not window.workspace_scroll.horizontalScrollBar().isVisible()


def test_gui_role_selection_clears_stale_reference_and_marks_missing(window):
    window.reference.setEditText("old-custom.wav")
    window._set_presets(
        [{"name": "角色", "language": "JA", "reference_exists": True}], "角色"
    )
    assert (
        window.reference.currentText() == "" and window.language.currentText() == "JA"
    )
    assert (
        window._payload()["preset"] == "角色"
        and window._payload()["reference_audio"] is None
    )
    window.reference.setEditText("custom.wav")
    window._temporary_reference_edited("custom.wav")
    assert (
        window._payload()["preset"] is None
        and window._payload()["reference_audio"] == "custom.wav"
    )
    window._set_presets([{"name": "损坏", "reference_exists": False}], "损坏")
    with pytest.raises(ValueError, match="管理当前角色"):
        window._payload()


def test_editor_import_rights_errors_and_save_preserves_original(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from voice_app.preset_dialog import PresetDialog

    app = QApplication.instance() or QApplication([])
    dialog = PresetDialog("http://127.0.0.1:1")
    dialog._save()
    assert "名称" in dialog.status.text()
    dialog.name.setText("新角色")
    dialog._save()
    assert "选择参考音频" in dialog.status.text()
    original = tmp_path / "voice.wav"
    original.write_bytes(sample())
    dialog.source_path = original
    dialog._save()
    assert "有权使用" in dialog.status.text()
    dialog.rights.setChecked(True)
    calls = []
    monkeypatch.setattr(
        dialog,
        "_request",
        lambda *args, **kw: calls.append((args, kw)) or {"name": "新角色"},
    )
    monkeypatch.setattr(dialog, "_run", lambda task, done, error=None: done(task()))
    dialog._save()
    assert (
        dialog.result_name == "新角色" and original.read_bytes() == calls[0][1]["data"]
    )
    assert calls[0][0] == ("POST", "/v1/presets")
    dialog.close()
    app.processEvents()


def test_editor_preview_recovers_from_short_audio_and_plays_long_clip(
    client, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication, QFileDialog

    from voice_app.preset_dialog import PresetDialog

    app = QApplication.instance() or QApplication([])
    short_audio, long_audio = tmp_path / "short.wav", tmp_path / "long.wav"
    short_audio.write_bytes(sample(0.5))
    long_audio.write_bytes(sample(65))
    files = iter([short_audio, long_audio])
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(next(files)), ""))
    played = []
    monkeypatch.setattr(
        client.app.state.preview,
        "play",
        lambda reference, device_id: played.append((reference.seconds, device_id)),
    )

    def request(method, url, *, timeout, data, **kwargs):
        path = url.removeprefix("http://127.0.0.1:1")
        assert path == "/v1/preset-preview"
        assert timeout[1] > 65
        assert "device_id" not in kwargs["params"]
        response = client.request(method, path, content=data, **kwargs)
        return SimpleNamespace(
            ok=response.is_success, status_code=response.status_code, json=response.json
        )

    monkeypatch.setattr("voice_app.preset_dialog.requests.request", request)
    dialog = PresetDialog(
        "http://127.0.0.1:1",
        devices=[{"name": "测试耳机", "host_api": "MME", "id": 16}],
    )
    dialog.show()

    def preview():
        dialog.preview_button.click()
        assert "正在检查参考音频" in dialog.status.text()
        assert QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        assert dialog.preview_button.isEnabled()
        assert not dialog._workers

    try:
        assert dialog.preview_button.text() == "播放参考音频"
        assert dialog.preview_device.currentText() == "跟随系统默认"
        dialog.browse.click()
        preview()
        assert dialog.status.isVisible()
        assert dialog.status.property("severity") == "error"
        assert dialog.status.text().startswith("试听失败：音频检查未通过，未播放。")
        assert "0.50 秒" in dialog.status.text() and "不足 1 秒" in dialog.status.text()
        assert played == []

        dialog.browse.click()
        assert dialog.status.property("severity") == "info"
        assert "已选择音频" in dialog.status.text()
        assert played == []
        preview()
        assert played == [(65, None)]
        assert "试听已结束" in dialog.status.text() and "65.00 秒" in dialog.status.text()
        assert not client.get("/health").json()["model_loaded"]
        assert client.get("/v1/presets").json() == []
    finally:
        dialog.close()


def test_real_qt_import_dialog_refreshes_selection_and_persists_after_restart(
    window, client, tmp_path, monkeypatch
):
    import threading
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from voice_app import desktop_qt
    from voice_app.preset_dialog import PresetDialog

    original = tmp_path / "my-recording.wav"
    original.write_bytes(sample())
    main_thread = threading.get_ident()
    request_threads, accepted_threads = [], []
    dialogs = []

    def factory(*args):
        dialog = PresetDialog(*args)
        dialogs.append(dialog)
        dialog.name.setText("新建测试角色")
        dialog.source_path = original
        dialog.file.setText(str(original))
        dialog.rights.setChecked(True)

        def request(method, path, **kwargs):
            request_threads.append(threading.get_ident())
            data = kwargs.pop("data", b"")
            response = client.request(method, path, content=data, **kwargs)
            response.raise_for_status()
            return response.json()

        dialog.accepted.connect(lambda: accepted_threads.append(threading.get_ident()))
        monkeypatch.setattr(dialog, "_request", request)
        QTimer.singleShot(0, dialog._save)
        # Fail closed if a regression leaves the modal dialog open.
        guard = QTimer(dialog)
        guard.setSingleShot(True)
        guard.timeout.connect(dialog.reject)
        dialog.finished.connect(guard.stop)
        guard.start(5000)
        return dialog

    def get(path):
        return client.get(path).json()

    def put(path, body):
        return client.put(path, json=body).json()

    monkeypatch.setattr(desktop_qt, "PresetDialog", factory)
    monkeypatch.setattr(window, "_get", get)
    monkeypatch.setattr(window, "_put", put)
    window._edit_preset(False)
    assert dialogs and dialogs[0].result_name == "新建测试角色", {
        "dialogs": len(dialogs),
        "requests": request_threads,
        "accepted": accepted_threads,
        "dialog_status": dialogs[0].status.text() if dialogs else "not opened",
        "window_status": window.status_pill.text(),
    }
    deadline = time.monotonic() + 5
    while window.preset.currentData() != "新建测试角色" and time.monotonic() < deadline:
        QApplication.processEvents()
        threading.Event().wait(0.01)
    assert window.preset.currentData() == "新建测试角色", window.status_pill.text()
    assert window._payload()["reference_audio"] is None
    assert window.generate_button.isEnabled()
    assert client.get("/v1/settings").json()["last_preset"] == "新建测试角色"
    original.unlink()
    store = PresetStore(client.app.state.runtime.resolver.store.root)
    assert store.summary("新建测试角色")["reference_exists"]
    assert request_threads and all(thread != main_thread for thread in request_threads)
    assert accepted_threads == [main_thread]
    assert not client.get("/health").json()["model_loaded"]


def test_async_failure_is_delivered_on_gui_thread_and_released(window):
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    main_thread = threading.get_ident()
    errors = []

    def fail():
        raise ValueError("fixture import failure")

    window._run_async(
        fail, on_error=lambda message: errors.append((message, threading.get_ident()))
    )
    deadline = time.monotonic() + 3
    while not errors and time.monotonic() < deadline:
        QApplication.processEvents()
        threading.Event().wait(0.01)
    assert errors == [("fixture import failure", main_thread)]
    assert not window._workers


def test_queued_async_result_does_not_update_closed_window(window):
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    started, release = threading.Event(), threading.Event()
    callbacks = []

    def task():
        started.set()
        release.wait(2)
        return "finished"

    window._run_async(task, callbacks.append)
    assert started.wait(1)
    window.close()
    release.set()
    deadline = time.monotonic() + 3
    while window._workers and time.monotonic() < deadline:
        QApplication.processEvents()
        threading.Event().wait(0.01)
    assert not callbacks and not window._workers
