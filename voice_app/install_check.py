"""Exercise the real installer and backend from the packaged executable.

Maintenance-only entry point; always requires an explicit test data directory.
No audio device is opened and no global application configuration is written.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import requests

from . import __version__
from .client_main import _find_uv, _installation_source, _start_backend, _stop_backend
from .engine_setup import install_managed_engine


def verify_install(root: Path, data_dir: Path, reference: Path | None) -> int:
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    report_path = data_dir / "verification.json"
    report = {
        "app_version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "installed": False,
        "real_load": False,
        "generated_pcm": False,
    }
    process = log = None

    def save_report():
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def progress(message):
        with (data_dir / "verification.log").open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    save_report()
    try:
        root, _ = install_managed_engine(
            root,
            data_dir,
            _find_uv(),
            _installation_source(),
            progress,
            threading.Event(),
        )
        report["installed"] = True
        save_report()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process, log = _start_backend(root, data_dir, port=port)
        base = f"http://127.0.0.1:{port}"
        with requests.Session() as session:
            session.trust_env = False
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(
                        "验证服务未能启动，请查看隔离数据目录的 backend.log"
                    )
                try:
                    response = session.get(base + "/health", timeout=1)
                    if response.ok:
                        assert response.json()["version"] == __version__
                        break
                except requests.RequestException:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("验证服务启动超时")
                time.sleep(0.25)
            response = session.post(base + "/v1/model/load", timeout=(5, 600))
            response.raise_for_status()
            report["real_load"] = response.json().get("model_loaded") is True
            if not report["real_load"]:
                raise RuntimeError("验证服务未确认模型加载")
            save_report()
            if reference:
                response = session.post(
                    base + "/v1/tts/stream",
                    json={
                        "text": "你好，这是安装测试。",
                        "reference_audio": str(reference.resolve()),
                        "language": "ZH",
                        "output_device": None,
                        "monitor_enabled": False,
                        "save_output": False,
                    },
                    timeout=(5, 600),
                )
                response.raise_for_status()
                if len(response.content) <= 4410:
                    raise RuntimeError("生成音频长度不足")
                report["generated_pcm"] = True
                report["pcm_bytes"] = len(response.content)
        progress("安装与真实模型自检通过。")
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        progress(report["error"])
        return 1
    finally:
        try:
            if process is not None:
                _stop_backend(process, log)
        finally:
            save_report()
