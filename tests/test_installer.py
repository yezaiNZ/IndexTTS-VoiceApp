import hashlib
import json
import threading
from dataclasses import replace

import pytest
import requests

from voice_app import __version__, downloads, engine_setup
from voice_app.http_errors import check_response
from voice_app.model_manifest import ALL_MODELS, MANIFEST_VERSION, ModelFile


DATA = b"verified voice model"
ITEM = ModelFile(
    "weights.bin",
    "test/model",
    "revision",
    "weights.bin",
    len(DATA),
    hashlib.sha256(DATA).hexdigest(),
)


class Reply:
    def __init__(self, data=DATA, status=200, headers=None, cancel=None):
        self.data, self.status_code = data, status
        self.headers, self.cancel = headers or {}, cancel

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, **kwargs):
        yield self.data[:3]
        if self.cancel:
            self.cancel.set()
        yield self.data[3:]


def fetch(tmp_path, cancel=None):
    destination = tmp_path / ITEM.path
    downloads.download_file(
        ITEM,
        destination,
        "auto",
        lambda _: None,
        cancel or threading.Event(),
        tmp_path / "log",
        urls=[("vendor", "https://example.invalid/model")],
    )
    return destination


def test_atomic_download_checks_content(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Reply())
    assert fetch(tmp_path).read_bytes() == DATA
    assert not (tmp_path / (ITEM.path + ".voice-app.part")).exists()


def test_valid_download_is_not_downloaded_again(tmp_path):
    (tmp_path / ITEM.path).write_bytes(DATA)
    assert fetch(tmp_path).read_bytes() == DATA


def test_resume_requests_correct_range(tmp_path, monkeypatch):
    (tmp_path / (ITEM.path + ".voice-app.part")).write_bytes(DATA[:5])

    def request(*args, **kwargs):
        assert kwargs["headers"]["Range"] == "bytes=5-"
        return Reply(DATA[5:], 206, {"Content-Range": f"bytes 5-19/{len(DATA)}"})

    monkeypatch.setattr(requests, "get", request)
    assert fetch(tmp_path).read_bytes() == DATA


def test_ignored_range_restarts_file(tmp_path, monkeypatch):
    (tmp_path / (ITEM.path + ".voice-app.part")).write_bytes(DATA[:5])
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Reply())
    assert fetch(tmp_path).read_bytes() == DATA


def test_wrong_range_keeps_partial(tmp_path, monkeypatch):
    partial = tmp_path / (ITEM.path + ".voice-app.part")
    partial.write_bytes(DATA[:5])
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: Reply(DATA[5:], 206, {"Content-Range": "bytes 0-19/20"}),
    )
    with pytest.raises(RuntimeError, match="已下载内容会保留"):
        fetch(tmp_path)
    assert partial.read_bytes() == DATA[:5]


def test_bad_hash_never_overwrites_original(tmp_path, monkeypatch):
    destination = tmp_path / ITEM.path
    destination.write_bytes(b"old model preserved")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Reply(b"x" * len(DATA)))
    with pytest.raises(RuntimeError):
        fetch(tmp_path)
    assert destination.read_bytes() == b"old model preserved"
    assert list(tmp_path.glob("*.invalid-*"))


def test_network_interruption_resumes_without_manual_cleanup(tmp_path, monkeypatch):
    attempts = []

    class Interrupted(Reply):
        def iter_content(self, **kwargs):
            yield DATA[:5]
            raise requests.ConnectionError("interrupted")

    def request(*args, **kwargs):
        attempts.append(kwargs["headers"].get("Range"))
        return (
            Interrupted()
            if len(attempts) == 1
            else Reply(DATA[5:], 206, {"Content-Range": "bytes 5-19/20"})
        )

    monkeypatch.setattr(requests, "get", request)
    assert fetch(tmp_path).read_bytes() == DATA
    assert attempts == [None, "bytes=5-"]


def test_cancellation_preserves_partial_download(tmp_path, monkeypatch):
    cancel = threading.Event()
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Reply(cancel=cancel))
    with pytest.raises(downloads.DownloadCancelled):
        fetch(tmp_path, cancel)
    assert (tmp_path / (ITEM.path + ".voice-app.part")).read_bytes() == DATA[:3]


def test_manifest_has_every_required_auxiliary_weight_and_feature():
    paths = {item.path for item in ALL_MODELS}
    assert {
        "feat1.pt",
        "feat2.pt",
        "hf_cache/w2v-bert-2.0/model.safetensors",
        "hf_cache/campplus_cn_common.bin",
        "hf_cache/bigvgan/bigvgan_generator.pt",
    } <= paths
    assert len(paths) == len(ALL_MODELS)
    assert all(item.size > 0 and len(item.sha256) == 64 for item in ALL_MODELS)


def test_existing_auxiliary_folder_is_not_treated_as_complete(tmp_path, monkeypatch):
    item = replace(ITEM, path="hf_cache/w2v-bert-2.0/model.safetensors")
    monkeypatch.setattr(engine_setup, "ALL_MODELS", [item])
    folder = tmp_path / "checkpoints" / "hf_cache" / "w2v-bert-2.0"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    assert engine_setup.model_inventory_errors(tmp_path) == [item.path]
    (folder / "model.safetensors").write_bytes(DATA)
    assert not engine_setup.model_inventory_errors(tmp_path, verify_hash=True)
    (folder / "model.safetensors").write_bytes(b"x" * len(DATA))
    assert engine_setup.model_inventory_errors(tmp_path, verify_hash=True) == [
        item.path
    ]


def test_config_becomes_ready_only_after_real_load(tmp_path, monkeypatch):
    from voice_app import client_main

    monkeypatch.setattr(
        client_main, "validate_engine", lambda root: (root, root / "python")
    )
    monkeypatch.setattr(client_main, "preparation_is_current", lambda root: True)
    client_main._save_config(tmp_path, tmp_path / "engine")
    assert not client_main.configuration_ready(client_main._load_config(tmp_path))
    client_main._save_config(tmp_path, tmp_path / "engine", ready=True)
    config = client_main._load_config(tmp_path)
    assert client_main.configuration_ready(config)
    assert not client_main.configuration_ready({**config, "backend_version": "0.3.2"})
    monkeypatch.setattr(client_main, "preparation_is_current", lambda root: False)
    assert not client_main.configuration_ready(config)


def test_receipt_requires_full_model_inventory(tmp_path, monkeypatch):
    (tmp_path / ".voice-app-prepared.json").write_text(
        json.dumps({"manifest": MANIFEST_VERSION})
    )
    monkeypatch.setattr(engine_setup, "ALL_MODELS", [ITEM])
    assert not engine_setup.preparation_is_current(tmp_path)
    (tmp_path / "checkpoints").mkdir()
    (tmp_path / "checkpoints" / ITEM.path).write_bytes(DATA)
    assert engine_setup.preparation_is_current(tmp_path)


def test_load_failure_does_not_mark_model_ready(monkeypatch):
    from voice_app.client_main import _LoadWorker

    response = requests.Response()
    response.status_code = 500
    response._content = b'{"detail":"missing auxiliary weight"}'
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    worker = _LoadWorker()
    worker.run()
    assert "missing auxiliary weight" in worker.error


def test_api_error_exposes_useful_detail():
    response = requests.Response()
    response.status_code = 500
    response._content = b'{"detail":"model incomplete"}'
    with pytest.raises(RuntimeError, match="model incomplete"):
        check_response(response)


def test_desktop_without_cable_uses_local_monitor(tmp_path, monkeypatch):
    from voice_app.desktop_qt import VoiceDesktopQt

    monkeypatch.setattr(VoiceDesktopQt, "_refresh_data", lambda self: None)
    window = VoiceDesktopQt("http://127.0.0.1:7862", model_ready=True)
    window._apply_data(
        {
            "presets": [],
            "inputs": [],
            "settings": {"monitor_enabled": False, "microphone_enabled": True},
            "outputs": [
                {
                    "name": "Headphones",
                    "host_api": "MME",
                    "id": 9,
                    "is_default": True,
                    "is_vb_cable": False,
                }
            ],
        }
    )
    window.text.setPlainText("测试")
    window.reference.setEditText(str(tmp_path / "sample.wav"))
    payload = window._payload()
    assert payload["output_device"] is None
    assert payload["monitor_enabled"] and payload["monitor_device"] == 9
    assert not window._mixer_payload()["enabled"]
    assert window.generate_button.isEnabled()
    window.close()


def test_desktop_generation_disabled_until_model_ready(monkeypatch):
    from voice_app.desktop_qt import VoiceDesktopQt

    monkeypatch.setattr(VoiceDesktopQt, "_refresh_data", lambda self: None)
    window = VoiceDesktopQt("http://127.0.0.1:7862")
    assert not window.generate_button.isEnabled()
    window.generate()
    assert not window.generating
    window._model_loaded({"model_loaded": True})
    assert window.generate_button.isEnabled()
    window._model_error("missing model")
    assert not window.generate_button.isEnabled()
    window.close()


def test_backend_source_matches_current_version(tmp_path, monkeypatch):
    from voice_app import client_main

    monkeypatch.setattr(client_main.sys, "executable", str(tmp_path / "app.exe"))
    (tmp_path / "index_tts_voice_app-0.3.2-py3-none-any.whl").touch()
    current = tmp_path / f"index_tts_voice_app-{__version__}-py3-none-any.whl"
    current.touch()
    assert client_main._installation_source() == [str(current)]


def test_malformed_configuration_enters_repair_instead_of_crashing(tmp_path):
    from voice_app.client_main import _load_config

    (tmp_path / "launcher.json").write_text("[]")
    assert _load_config(tmp_path) == {}
    (tmp_path / ".voice-app-prepared.json").write_text("[]")
    assert not engine_setup.preparation_is_current(tmp_path)
