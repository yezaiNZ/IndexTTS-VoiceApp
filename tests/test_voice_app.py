import json
import hashlib
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voice_app.api import create_app
from voice_app.audio import AudioBuffer, _stable_cable_device, convert_pcm
from voice_app.presets import PresetResolver
from voice_app.schemas import AudioDevice, DesktopSettings, SynthesisRequest
from voice_app.settings import SettingsStore


def test_request_requires_preset_or_reference():
    with pytest.raises(ValidationError):
        SynthesisRequest(text="hello")


def test_blank_text_is_rejected_after_trimming(tmp_path):
    prompt = tmp_path / "prompt.wav"
    prompt.touch()
    resolver = PresetResolver()
    with pytest.raises(ValueError, match="blank"):
        resolver.resolve(SynthesisRequest(text="   ", reference_audio=str(prompt)))


def test_preset_resolver_merges_explicit_generation_values(tmp_path):
    prompt = tmp_path / "prompt.wav"
    prompt.touch()
    preset = {
        "prompt_audio": str(prompt),
        "emo_control_method": 0,
        "emo_weight": 0.52,
        "advanced_params": {"top_p": 0.6, "top_k": 20},
    }
    resolver = PresetResolver(loader=lambda name: preset if name == "voice" else None)
    request = SynthesisRequest(
        text="hello",
        preset="voice",
        generation={"top_p": 0.9},
    )

    resolved = resolver.resolve(request)

    assert resolved.reference_audio == str(prompt.resolve())
    assert resolved.emotion_weight == 0.52
    assert resolved.generation["top_p"] == 0.9
    assert resolved.generation["top_k"] == 20
    assert resolved.generation["temperature"] == 0.8


def test_reference_audio_overrides_preset(tmp_path):
    preset_prompt = tmp_path / "preset.wav"
    explicit_prompt = tmp_path / "explicit.wav"
    preset_prompt.touch()
    explicit_prompt.touch()
    resolver = PresetResolver(loader=lambda _: {"prompt_audio": str(preset_prompt)})
    request = SynthesisRequest(
        text="hello",
        preset="voice",
        reference_audio=str(explicit_prompt),
    )

    resolved = resolver.resolve(request)

    assert resolved.reference_audio == str(explicit_prompt.resolve())


def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    expected = DesktopSettings(
        output_device=12,
        monitor_enabled=True,
        monitor_device=7,
        last_preset="voice",
        language="JA",
    )

    store.save(expected)

    assert store.load() == expected
    assert Path(store.path).is_file()


def test_stream_endpoint_uses_existing_runtime_instance(tmp_path):
    torch = pytest.importorskip("torch")

    class FakeModel:
        def normalize_emo_vec(self, values, apply_bias=True):
            return values

        def infer(self, **kwargs):
            yield torch.tensor([[0.0, 1.0, -1.0]])

    prompt = tmp_path / "prompt.wav"
    prompt.touch()
    app = create_app(settings_path=tmp_path / "settings.json")
    app.state.runtime._model = FakeModel()

    with TestClient(app) as client:
        response = client.post(
            "/v1/tts/stream",
            json={
                "text": "hello",
                "reference_audio": str(prompt),
                "save_output": False,
            },
        )
        health = client.get("/health").json()

    assert response.status_code == 200
    assert response.content == b"\x00\x00\x01\x00\xff\xff"
    assert response.headers["x-audio-sample-rate"] == "22050"
    assert health["model_loaded"] is True
    assert health["busy"] is False


def test_pcm_conversion_uses_device_rate_and_duplicates_stereo():
    import numpy as np

    source = np.array([0, 1000, -1000, 2000, -2000], dtype="<i2").tobytes()

    converted = convert_pcm(source, source_rate=22050, target_rate=44100, channels=2)

    assert converted.dtype == np.int16
    assert converted.shape == (10, 2)
    assert np.array_equal(converted[:, 0], converted[:, 1])
    assert np.max(np.abs(converted)) <= 32767


def test_other_cable_backends_fall_back_to_verified_mme_device():
    devices = [
        AudioDevice(
            id=42,
            name="CABLE Input",
            host_api="Windows WASAPI",
            channels=2,
            default_samplerate=48000,
            is_vb_cable=True,
        ),
        AudioDevice(
            id=13,
            name="CABLE Input",
            host_api="MME",
            channels=8,
            default_samplerate=44100,
            is_vb_cable=True,
        ),
        AudioDevice(
            id=32,
            name="CABLE Input",
            host_api="Windows DirectSound",
            channels=8,
            default_samplerate=44100,
            is_vb_cable=True,
        ),
    ]

    assert _stable_cable_device(42, devices) == 13
    assert _stable_cable_device(32, devices) == 13


def test_audio_buffer_preserves_order_and_zero_fills():
    import numpy as np

    buffer = AudioBuffer()
    buffer.write(np.array([0.1, 0.2], dtype=np.float32))
    buffer.write(np.array([0.3], dtype=np.float32))

    first = buffer.read(2)
    second = buffer.read(3)

    assert np.allclose(first, [0.1, 0.2])
    assert np.allclose(second, [0.3, 0.0, 0.0])
    assert buffer.pending_frames == 0
    assert buffer.wait_empty(timeout=0)


def test_audio_buffer_can_discard_pending_tts():
    import numpy as np

    buffer = AudioBuffer()
    buffer.write(np.ones(20, dtype=np.float32))
    buffer.clear()

    assert buffer.pending_frames == 0
    assert np.count_nonzero(buffer.read(20)) == 0


def test_qt_frontend_imports():
    from voice_app.desktop_qt import VoiceDesktopQt

    assert VoiceDesktopQt.__name__ == "VoiceDesktopQt"


def test_setup_dialog_contrast_and_engine_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    from voice_app import __version__
    from voice_app.client_main import SetupDialog

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    dialog = SetupDialog(tmp_path)
    dialog.show()
    app.processEvents()

    assert f"v{__version__}" in dialog.windowTitle()
    assert Path(dialog.path.text()).name == "IndexTTS-2.5-engine"
    assert dialog.source_combo.currentData() == "auto"
    assert dialog.source_combo.count() == 3
    for option in (dialog.auto, dialog.existing, dialog.license):
        assert option.palette().color(QPalette.ColorRole.WindowText).name() == "#26334f"
    dialog.close()


def test_first_run_engine_check_finds_nested_source_and_reports_missing_model(tmp_path):
    from voice_app.client_main import validate_engine

    engine = tmp_path / "index-tts"
    (engine / "indextts").mkdir(parents=True)
    (engine / "indextts" / "infer_v2_5.py").touch()
    (engine / ".venv" / "Scripts").mkdir(parents=True)
    (engine / ".venv" / "Scripts" / "python.exe").touch()

    with pytest.raises(ValueError, match="checkpoints/config.yaml"):
        validate_engine(tmp_path)

    (engine / "checkpoints").mkdir()
    (engine / "checkpoints" / "config.yaml").touch()
    root, python = validate_engine(tmp_path)

    assert root == engine
    assert python == engine / ".venv" / "Scripts" / "python.exe"


def test_managed_install_refuses_existing_unmarked_directory(tmp_path):
    from voice_app.engine_setup import check_managed_destination

    destination = tmp_path / "engine"
    destination.mkdir()
    with pytest.raises(ValueError, match="已存在"):
        check_managed_destination(destination)


def test_managed_install_can_resume_and_never_redownload_source(tmp_path, monkeypatch):
    from voice_app import engine_setup as setup
    from types import SimpleNamespace

    monkeypatch.setattr(
        setup.shutil, "disk_usage", lambda _: SimpleNamespace(free=50 * 1024**3)
    )

    monkeypatch.setattr(
        setup,
        "MODEL_SHA256",
        {name: hashlib.sha256(b"test").hexdigest() for name in setup.MODEL_FILES},
    )
    destination = tmp_path / "engine"
    data_dir = tmp_path / "data"
    calls = []

    def fake_download(archive, cancel, progress):
        archive.parent.mkdir(parents=True)
        prefix = f"index-tts-{setup.SOURCE_COMMIT}/"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(prefix + "indextts/infer_v2_5.py", "# test")
            bundle.writestr(prefix + "pyproject.toml", "# test")

    def fake_run(command, root, environment, log_path, cancel, progress):
        calls.append(command)
        if command[1] == "sync":
            python = root / setup.APP_ENVIRONMENT / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        elif command[1:3] == ["tool", "run"]:
            model = root / "checkpoints"
            model.mkdir(exist_ok=True)
            for name in setup.MODEL_FILES:
                (model / name).write_bytes(b"test")

    monkeypatch.setattr(setup, "_download_source", fake_download)
    monkeypatch.setattr(setup, "_run_command", fake_run)
    monkeypatch.setattr(
        setup, "_ensure_python", lambda root, *args: setup._physical_python(root)
    )
    monkeypatch.setattr(
        setup,
        "_ensure_app_environment",
        lambda root, *args: root / setup.APP_ENVIRONMENT / "Scripts" / "python.exe",
    )
    monkeypatch.setattr(setup, "_verify_app_python", lambda *args: None)

    def fake_model_download(item, destination, source, progress, cancel, log_path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"test")

    monkeypatch.setattr(setup, "download_file", fake_model_download)
    cancel = threading.Event()
    root, python = setup.install_managed_engine(
        destination, data_dir, "uv", ["backend.whl"], lambda _: None, cancel
    )

    assert root == destination
    assert python.is_file()
    assert setup.is_managed_engine(root)
    assert (
        json.loads((root / setup.MARKER).read_text())["source_commit"]
        == setup.SOURCE_COMMIT
    )
    assert [command[1] for command in calls] == ["sync", "-c", "pip"]
    # Our validated environment is uv-managed. Excluding managed environments
    # here would make uv unnecessarily recreate it and reinstall every package.
    assert "--no-managed-python" not in calls[0]
    assert "--no-python-downloads" in calls[0]
    assert calls[0][calls[0].index("--python") + 1] == str(python)
    assert python.parent.parent.name == setup.APP_ENVIRONMENT

    def unexpected_download(*args):
        raise AssertionError("Source was redownloaded")

    monkeypatch.setattr(setup, "_download_source", unexpected_download)
    setup.install_managed_engine(
        destination, data_dir, "uv", ["backend.whl"], lambda _: None, cancel
    )


def test_model_download_falls_back_to_modelscope(tmp_path, monkeypatch):
    from voice_app.downloads import download_urls
    from voice_app.model_manifest import PRIMARY_MODELS

    urls = download_urls(PRIMARY_MODELS[0], "auto")
    assert urls[0][0] == "ModelScope"
    assert urls[1][0] == "Hugging Face"
    assert urls[2][0] == "校验镜像"


def test_model_download_failure_is_short_and_actionable(tmp_path, monkeypatch):
    from voice_app import downloads
    from voice_app.model_manifest import PRIMARY_MODELS

    def fail_request(*args, **kwargs):
        raise downloads.requests.ConnectionError("offline")

    monkeypatch.setattr(downloads.requests, "get", fail_request)
    with pytest.raises(RuntimeError, match="已下载内容会保留") as error:
        downloads.download_file(
            PRIMARY_MODELS[0],
            tmp_path / "LICENSE",
            "auto",
            lambda _: None,
            threading.Event(),
            tmp_path / "setup.log",
        )
    assert "Traceback" not in str(error.value)


def test_managed_install_can_cancel_before_touching_destination(tmp_path):
    from voice_app.engine_setup import InstallCancelled, install_managed_engine

    destination = tmp_path / "engine"
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InstallCancelled):
        install_managed_engine(
            destination,
            tmp_path / "data",
            "uv",
            ["backend.whl"],
            lambda _: None,
            cancelled,
        )
    assert not destination.exists()
