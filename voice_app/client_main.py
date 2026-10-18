"""Desktop launcher: install or reuse IndexTTS and start the local API."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from . import __version__
from .desktop_qt import VoiceDesktopQt
from .downloads import DownloadCancelled
from .driver_setup import cable_is_installed, install_bundled_driver
from .engine_setup import (
    engine_environment,
    engine_python,
    install_managed_engine,
    is_managed_engine,
    preparation_is_current,
    prepare_existing_engine,
)
from .http_errors import check_response
from .lifecycle import BackendOwnership
from .model_manifest import MANIFEST_VERSION
from .processes import external_program, independent_environment

APP_NAME = "IndexTTSVoiceApp"
PORT = 7862


def user_data_dir() -> Path:
    override = os.environ.get("VOICE_APP_USER_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def default_engine_root(data_dir: Path) -> Path:
    """Prefer a roomy non-system drive without asking users to locate Python."""
    for drive in ("D:/", "E:/", "F:/"):
        try:
            if Path(drive).is_dir() and shutil.disk_usage(drive).free >= 25 * 1024**3:
                return Path(drive) / "IndexTTS-2.5-engine"
        except OSError:
            continue
    return data_dir / "IndexTTS-2.5-engine"


def validate_engine(root: Path) -> tuple[Path, Path]:
    """Return the actual source root and its Python, or a useful setup error."""
    root = root.expanduser().resolve()
    if (root / "index-tts" / "indextts" / "infer_v2_5.py").is_file():
        root = root / "index-tts"
    if not (root / "indextts" / "infer_v2_5.py").is_file():
        raise ValueError(
            "没有找到 IndexTTS 2.5 推理源码；请选择包含 indextts 文件夹的目录。"
        )
    if not (root / "checkpoints" / "config.yaml").is_file():
        raise ValueError("没有找到 checkpoints/config.yaml；请先下载并放好模型。")
    python = engine_python(root)
    if not python.is_file():
        raise ValueError("没有找到可用的推理环境；请通过自动安装向导准备或修复。")
    return root, python


def _installation_source() -> list[str]:
    executable_dir = Path(sys.executable).resolve().parent
    wheels = sorted(executable_dir.glob(f"index_tts_voice_app-{__version__}-*.whl"))
    if wheels:
        return [str(wheels[-1])]
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "pyproject.toml").is_file():
        return ["-e", str(source_root)]
    raise RuntimeError("安装包缺少后端 wheel，请重新下载完整的安装包。")


def _find_uv() -> str:
    bundled = Path(sys.executable).resolve().parent / "uv.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("uv")
    if found:
        return found
    raise RuntimeError("没有找到 uv。源码运行请先安装 uv，再重新运行安装脚本。")


def install_backend(engine_python: Path, data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    environment = independent_environment(os.environ)
    environment["UV_CACHE_DIR"] = str(data_dir / "cache")
    command = [
        _find_uv(),
        "pip",
        "install",
        "--python",
        str(engine_python),
        *_installation_source(),
    ]
    with external_program():
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env=environment,
            check=False,
        )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError("后端依赖安装失败：\n" + detail[-1800:])


def _load_config(data_dir: Path) -> dict:
    path = data_dir / "launcher.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(data_dir: Path, root: Path, ready: bool = False):
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "launcher.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "index_tts_root": str(root),
                "backend_version": __version__,
                "manifest": MANIFEST_VERSION,
                "ready": ready,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _migrate_local_data(data_dir: Path):
    """Keep the existing developer installation's private presets and settings."""
    source = Path(__file__).resolve().parent.parent
    if not (source / "pyproject.toml").is_file():
        return
    old_presets = source / "presets"
    new_presets = data_dir / "presets"
    if old_presets.is_dir() and not new_presets.exists():
        shutil.copytree(old_presets, new_presets)
    old_settings = source / "outputs" / "settings.json"
    new_settings = data_dir / "outputs" / "settings.json"
    if old_settings.is_file() and not new_settings.exists():
        new_settings.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_settings, new_settings)


class _InstallSignals(QObject):
    complete = Signal()
    error = Signal(str)
    progress = Signal(str)
    cancelled = Signal(str)


class _InstallWorker(QRunnable):
    def __init__(
        self,
        root: Path,
        python: Path | None,
        data_dir: Path,
        model_source: str,
        install_driver: bool = False,
    ):
        super().__init__()
        self.root = root
        self.python = python
        self.data_dir = data_dir
        self.model_source = model_source
        self.install_driver = install_driver
        self.cancel_event = threading.Event()
        self.signals = _InstallSignals()

    @Slot()
    def run(self):
        try:
            if self.install_driver:
                install_bundled_driver(
                    Path(sys.executable).parent,
                    self.data_dir,
                    self.signals.progress.emit,
                    self.cancel_event,
                )
            if self.python is None:
                install_managed_engine(
                    self.root,
                    self.data_dir,
                    _find_uv(),
                    _installation_source(),
                    self.signals.progress.emit,
                    self.cancel_event,
                    model_source=self.model_source,
                )
            else:
                prepare_existing_engine(
                    self.root,
                    self.data_dir,
                    _find_uv(),
                    _installation_source(),
                    self.signals.progress.emit,
                    self.cancel_event,
                    self.model_source,
                )
        except DownloadCancelled as exc:
            self.signals.cancelled.emit(str(exc))
        except Exception as exc:
            self.signals.error.emit(str(exc))
        else:
            self.signals.complete.emit()


class SetupDialog(QDialog):
    def __init__(self, data_dir: Path, previous: str = ""):
        super().__init__()
        self.data_dir = data_dir
        self.selected_root: Path | None = None
        self._auto_path = (
            previous
            if previous and is_managed_engine(Path(previous))
            else str(default_engine_root(data_dir))
        )
        self._existing_path = previous or os.environ.get("INDEXTTS_ROOT", "")
        self.setWindowTitle(f"安装与修复 · IndexTTS Voice App v{__version__}")
        self.setMinimumWidth(680)
        self.setStyleSheet(
            """
            QDialog { background: #f1f4fa; }
            QFrame#setupHeader {
                background: #202c49; border: none; border-radius: 18px;
            }
            QFrame#setupCard {
                background: #ffffff; border: 1px solid #dce4f0;
                border-radius: 16px;
            }
            QLabel { color: #26334f; font-size: 12px; }
            QLabel#setupTitle { color: #ffffff; font-size: 21px; font-weight: 700; }
            QLabel#setupSubtitle { color: #d4def0; font-size: 12px; }
            QLabel#setupVersion { color: #a8bdfb; font-size: 11px; font-weight: 600; }
            QLabel#sectionTitle { color: #26334f; font-size: 14px; font-weight: 700; }
            QLabel#pathLabel { color: #475774; font-weight: 600; }
            QLabel#modeHint {
                color: #34466c; background: #eef3ff;
                border: 1px solid #dae5ff; border-radius: 9px;
                padding: 9px 11px;
            }
            QRadioButton, QCheckBox {
                color: #26334f; background: transparent;
                font-size: 12px; spacing: 9px;
            }
            QRadioButton::indicator, QCheckBox::indicator {
                width: 17px; height: 17px;
            }
            QRadioButton::indicator {
                border-radius: 9px; border: 2px solid #8b9bb7;
                background: #ffffff;
            }
            QRadioButton::indicator:checked {
                border: 5px solid #526edb;
            }
            QCheckBox::indicator {
                border-radius: 4px; border: 2px solid #8b9bb7;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                border-color: #526edb; background: #526edb;
            }
            QRadioButton:disabled, QCheckBox:disabled { color: #687996; }
            QLineEdit {
                color: #26334f; background: #ffffff; padding: 10px;
                border: 1px solid #cbd5e1; border-radius: 9px;
                selection-background-color: #526edb;
            }
            QLineEdit:focus { border: 1px solid #526edb; }
            QComboBox {
                color: #26334f; background: #ffffff; padding: 9px;
                border: 1px solid #cbd5e1; border-radius: 9px;
            }
            QComboBox QAbstractItemView {
                color: #26334f; background: #ffffff;
                selection-background-color: #e8eeff;
            }
            QPushButton {
                color: #26334f; background: #eef2fa;
                padding: 10px 15px; border: 1px solid #d4dceb;
                border-radius: 9px;
            }
            QPushButton:hover { background: #e2e9f8; }
            QPushButton:disabled { color: #69768f; background: #e8ecf3; }
            QPushButton#primaryButton {
                background: #526edb; color: #ffffff;
                border: 1px solid #526edb; font-weight: 700;
            }
            QPushButton#primaryButton:hover { background: #405bc6; }
            QPushButton#primaryButton:disabled {
                background: #aebbe9; border-color: #aebbe9;
                color: #26334f;
            }
            QProgressBar { border: none; border-radius: 5px; background: #dfe6f3; }
            QProgressBar::chunk { background: #526edb; border-radius: 5px; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        header = QFrame()
        header.setObjectName("setupHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 18, 22, 18)
        header_layout.setSpacing(5)
        title = QLabel("准备语音引擎")
        title.setObjectName("setupTitle")
        header_layout.addWidget(title)
        subtitle = QLabel(
            "一键准备 Python、CUDA 推理依赖和全部模型，完成真实自检后即可使用。"
        )
        subtitle.setObjectName("setupSubtitle")
        header_layout.addWidget(subtitle)
        version = QLabel(f"VOICE APP v{__version__}  ·  引擎 IndexTTS 2.5")
        version.setObjectName("setupVersion")
        header_layout.addWidget(version)
        layout.addWidget(header)

        card = QFrame()
        card.setObjectName("setupCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(12)
        section = QLabel("选择引擎来源")
        section.setObjectName("sectionTitle")
        card_layout.addWidget(section)
        self.auto = QRadioButton("自动安装（推荐）")
        self.existing = QRadioButton("已有引擎（高级：自动检查并补齐缺失文件）")
        self.auto.toggled.connect(self._switch_mode)
        card_layout.addWidget(self.auto)
        card_layout.addWidget(self.existing)
        self.hint = QLabel()
        self.hint.setObjectName("modeHint")
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(64)
        card_layout.addWidget(self.hint)
        self.source_label = QLabel("模型下载来源")
        self.source_label.setObjectName("pathLabel")
        card_layout.addWidget(self.source_label)
        self.source_combo = QComboBox()
        self.source_combo.addItem("自动选择可用来源（推荐，无需配置）", "auto")
        self.source_combo.addItem("优先 ModelScope", "modelscope")
        self.source_combo.addItem("Hugging Face", "huggingface")
        card_layout.addWidget(self.source_combo)
        path_label = QLabel("IndexTTS 2.5 引擎目录（不是 Voice App 安装目录）")
        path_label.setObjectName("pathLabel")
        card_layout.addWidget(path_label)
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("选择一个有足够空间的引擎目录")
        row.addWidget(self.path, 1)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        card_layout.addLayout(row)
        self.license = QCheckBox("我已阅读并同意 IndexTTS 与辅助模型的上游许可协议")
        card_layout.addWidget(self.license)
        self.driver = QCheckBox(
            "同时安装 VB-Audio VB-CABLE（虚拟麦克风；会请求管理员许可）"
        )
        self.driver.setChecked(not cable_is_installed())
        self.driver.setVisible(not cable_is_installed())
        card_layout.addWidget(self.driver)
        driver_notice = QLabel(
            "VB-CABLE 为 VB-Audio 的 Donationware，可自愿支持作者。"
            ' <a href="https://vb-audio.com/Services/licensing.htm">来源与许可</a>。'
            "驱动安装可能要求重启；也可不安装，先使用本地试听。"
        )
        driver_notice.setWordWrap(True)
        driver_notice.setOpenExternalLinks(True)
        card_layout.addWidget(driver_notice)
        license_link = QLabel(
            '<a href="https://github.com/index-tts/index-tts/blob/'
            'ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c/LICENSE">'
            "查看 IndexTTS 许可协议</a> · "
            '<a href="https://huggingface.co/IndexTeam/IndexTTS-2.5">'
            "Hugging Face 模型页</a> · "
            '<a href="https://www.modelscope.cn/models/IndexTeam/IndexTTS-2.5">'
            "ModelScope 模型页</a>"
        )
        license_link.setOpenExternalLinks(True)
        card_layout.addWidget(license_link)
        layout.addWidget(card)
        self.stage = QLabel("准备安装；无需另行安装 Python 或 IndexTTS")
        layout.addWidget(self.stage)
        self.status = QLabel("准备检查")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = QPushButton("取消")
        self.cancel.clicked.connect(self.reject)
        self.open_log = QPushButton("查看安装日志")
        self.open_log.clicked.connect(self._open_log)
        self.open_log.hide()
        buttons.addWidget(self.open_log)
        buttons.addWidget(self.cancel)
        self.apply = QPushButton("开始")
        self.apply.setObjectName("primaryButton")
        self.apply.clicked.connect(self._install)
        buttons.addWidget(self.apply)
        layout.addLayout(buttons)
        self.installing = False
        if previous and not is_managed_engine(Path(previous)):
            self.existing.setChecked(True)
            self._switch_mode(False)
        else:
            self.auto.setChecked(True)

    def _switch_mode(self, checked: bool):
        if checked:
            self._existing_path = self.path.text() or self._existing_path
            self.path.setText(self._auto_path)
            self.hint.setText(
                "自动安装 Python、CUDA 推理依赖，以及约 7.1 GB 的全部必需模型。"
                "首次建议预留至少 25 GB，需联网；中断后重试可继续。"
                "旧版残缺环境会自动修复。"
            )
            self.license.show()
            self.source_label.show()
            self.source_combo.show()
            self.apply.setText("下载并安装")
        else:
            self._auto_path = self.path.text() or self._auto_path
            self.path.setText(self._existing_path)
            self.hint.setText(
                "选择已有 IndexTTS 2.5 目录。程序会校验并补齐必需模型，不替换您的源码。"
            )
            self.license.show()
            self.source_label.show()
            self.source_combo.show()
            self.apply.setText("检查、修复并使用")

    def _open_log(self):
        log_path = self.data_dir / "setup.log"
        if log_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path)))
        else:
            QMessageBox.information(self, "安装日志", "还没有生成安装日志。")

    def _browse(self):
        if self.auto.isChecked():
            directory = QFileDialog.getExistingDirectory(
                self, "选择安装位置的上一级目录", str(Path(self.path.text()).parent)
            )
            if directory:
                self.path.setText(str(Path(directory) / "IndexTTS-2.5-engine"))
            return
        directory = QFileDialog.getExistingDirectory(
            self, "选择 IndexTTS 2.5 目录", self.path.text()
        )
        if directory:
            self.path.setText(directory)

    def _install(self):
        try:
            if not self.license.isChecked():
                raise ValueError("安装或修复前，请阅读并同意上游许可协议。")
            if not self.path.text().strip():
                raise ValueError("请先选择目录。")
            if self.auto.isChecked():
                if not self.license.isChecked():
                    raise ValueError("自动下载前，请阅读并同意上游模型许可协议。")
                from .engine_setup import check_managed_destination

                root = check_managed_destination(Path(self.path.text().strip()))
                python = None
            else:
                root = Path(self.path.text().strip()).expanduser().resolve()
                if (root / "index-tts" / "indextts" / "infer_v2_5.py").is_file():
                    root = root / "index-tts"
                python = engine_python(root)
                if (
                    not python.is_file()
                    or not (root / "indextts" / "infer_v2_5.py").is_file()
                ):
                    raise ValueError("已有引擎缺少源码或 Python；请选择自动安装。")
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.selected_root = root
        self.installing = True
        self.apply.setEnabled(False)
        self.path.setEnabled(False)
        self.auto.setEnabled(False)
        self.existing.setEnabled(False)
        self.license.setEnabled(False)
        self.source_combo.setEnabled(False)
        self.driver.setEnabled(False)
        self.open_log.hide()
        self.cancel.setText("取消安装" if python is None else "请稍候…")
        self.cancel.setEnabled(True)
        self.progress.show()
        self.status.setText("正在准备安装…")
        self.worker = _InstallWorker(
            root,
            python,
            self.data_dir,
            self.source_combo.currentData(),
            self.driver.isChecked(),
        )
        self.worker.signals.complete.connect(self._done)
        self.worker.signals.error.connect(self._error)
        self.worker.signals.progress.connect(self._progress)
        self.worker.signals.cancelled.connect(self._error)
        QThreadPool.globalInstance().start(self.worker)

    def _done(self):
        _save_config(self.data_dir, self.selected_root)
        self.accept()

    def _progress(self, message: str):
        self.status.setText(message)
        if message.startswith("["):
            self.stage.setText(message)
        percentage = re.search(r"(\d+(?:\.\d+)?)%", message)
        if percentage:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, round(float(percentage.group(1)))))
        else:
            self.progress.setRange(0, 0)

    def _error(self, message: str):
        self.installing = False
        self.apply.setEnabled(True)
        self.path.setEnabled(True)
        self.cancel.setEnabled(True)
        self.cancel.setText("取消")
        self.auto.setEnabled(True)
        self.existing.setEnabled(True)
        self.license.setEnabled(True)
        self.source_combo.setEnabled(True)
        self.driver.setEnabled(True)
        self.apply.setText("重试安装 / 修复")
        self.progress.hide()
        self.status.setText(message)
        self.open_log.setVisible((self.data_dir / "setup.log").is_file())

    def reject(self):
        if self.installing:
            self.worker.cancel_event.set()
            self.cancel.setEnabled(False)
            self.status.setText("正在取消，请稍候…")
        else:
            super().reject()


def _check_port_available(port: int):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"本地端口 {port} 已被占用。请先退出旧版 Voice App，再重新启动。"
            ) from exc


def _start_backend(
    root: Path, data_dir: Path, port: int = PORT
) -> tuple[subprocess.Popen, object]:
    _check_port_available(port)
    _, python = validate_engine(root)
    data_dir.mkdir(parents=True, exist_ok=True)
    log = (data_dir / "backend.log").open("a", encoding="utf-8")
    environment = engine_environment(root, data_dir)
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["INDEXTTS_ROOT"] = str(root)
    command = [
        str(python),
        "-m",
        "voice_app",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--index-tts-root",
        str(root),
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    ownership = None
    process = None
    try:
        if os.name == "nt":
            ownership = BackendOwnership()
            command.extend(ownership.arguments())
        with external_program():
            process = subprocess.Popen(
                command,
                cwd=str(root),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        if ownership is not None:
            ownership.attach(process)
            process._voice_app_ownership = ownership
    except Exception:
        if ownership is not None:
            ownership.close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        log.close()
        raise
    return process, log


def _wait_for_backend(process: subprocess.Popen, app: QApplication) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("本地服务启动失败；请查看用户数据目录中的 backend.log。")
        try:
            response = requests.get(f"http://127.0.0.1:{PORT}/health", timeout=0.3)
            if response.ok and response.json().get("ok"):
                return
        except requests.RequestException:
            pass
        app.processEvents()
        time.sleep(0.15)
    raise RuntimeError("本地服务启动超时；请查看用户数据目录中的 backend.log。")


def _stop_backend(process: subprocess.Popen, log):
    try:
        ownership = getattr(process, "_voice_app_ownership", None)
        if ownership is not None:
            ownership.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        log.close()


class _LoadWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = _InstallSignals()
        self.error = ""

    @Slot()
    def run(self):
        try:
            response = requests.post(
                f"http://127.0.0.1:{PORT}/v1/model/load", timeout=(5, 600)
            )
            check_response(response)
            if not response.json().get("model_loaded"):
                raise RuntimeError("服务未确认模型加载完成，不能进入生成界面。")
        except Exception as exc:
            self.error = str(exc)
        try:
            self.signals.complete.emit()
        except RuntimeError:
            pass  # The user cancelled and the dialog has already closed.


class ModelCheckDialog(QDialog):
    """Verify the very same backend instance the desktop will use."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("最后一步 · 验证语音引擎")
        self.setMinimumWidth(520)
        self.setStyleSheet(
            "QDialog { background: #f1f4fa; }"
            "QLabel { color: #26334f; font-size: 13px; padding: 12px; }"
            "QPushButton { color: #26334f; background: #eef2fa; padding: 10px; }"
            "QProgressBar { background: #dfe6f3; border: none; min-height: 10px; }"
            "QProgressBar::chunk { background: #526edb; }"
        )
        layout = QVBoxLayout(self)
        label = QLabel(
            "正在真实加载全部语音模型并检查推理环境…\n完成后即可使用，请稍候。"
        )
        layout.addWidget(label)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        self.cancel = QPushButton("取消准备")
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel)
        self.worker = _LoadWorker()
        self.worker.signals.complete.connect(self.accept)

    def run_check(self):
        QThreadPool.globalInstance().start(self.worker)
        if self.exec() != QDialog.DialogCode.Accepted:
            raise DownloadCancelled("准备已取消；下次打开程序可以继续。")
        if self.worker.error:
            raise RuntimeError(self.worker.error)


def configuration_ready(config: dict) -> bool:
    try:
        root, _ = validate_engine(Path(config.get("index_tts_root", "")))
        return (
            config.get("backend_version") == __version__
            and config.get("ready") is True
            and config.get("manifest") == MANIFEST_VERSION
            and preparation_is_current(root)
        )
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IndexTTS Voice App desktop launcher")
    parser.add_argument(
        "--setup", action="store_true", help="open first-run setup again"
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="complete environment installation and real model self-test",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="isolated user data directory, useful for clean installation tests",
    )
    parser.add_argument("--verify-install", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reference-audio", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.verify_install:
        if not args.data_dir:
            parser.error("--verify-install requires an isolated --data-dir")
        from .install_check import verify_install

        return verify_install(args.verify_install, args.data_dir, args.reference_audio)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    data_dir = args.data_dir or user_data_dir()
    if not args.data_dir:
        _migrate_local_data(data_dir)
    config = _load_config(data_dir)
    root_text = config.get("index_tts_root", "")
    valid = configuration_ready(config)
    try:
        _check_port_available(PORT)
    except RuntimeError as exc:
        QMessageBox.warning(None, "请先关闭旧实例", str(exc))
        return 1
    while True:
        if args.setup or not valid:
            dialog = SetupDialog(data_dir, root_text)
            if root_text and not valid:
                dialog.status.setText(
                    "旧环境尚未通过完整检查，点击开始即可自动补齐并验证；无需删除环境。"
                )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return 2 if args.install else 0
            root = dialog.selected_root
            root_text = str(root)
        else:
            root = Path(root_text)
        process = None
        log = None
        progress = QProgressDialog("正在启动本地语音服务…", "", 0, 0)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        try:
            process, log = _start_backend(root, data_dir)
            _wait_for_backend(process, app)
            progress.close()
            ModelCheckDialog().run_check()
            _save_config(data_dir, root, ready=True)
            if args.install:
                _stop_backend(process, log)
                return 0
            window = VoiceDesktopQt(
                f"http://127.0.0.1:{PORT}",
                on_close=lambda: _stop_backend(process, log),
                model_ready=True,
            )
            try:
                return window.run()
            finally:
                _stop_backend(process, log)
        except Exception as exc:
            progress.close()
            if process is not None:
                _stop_backend(process, log)
            _save_config(data_dir, root, ready=False)
            if isinstance(exc, DownloadCancelled):
                return 2 if args.install else 0
            data_dir.mkdir(parents=True, exist_ok=True)
            with (data_dir / "setup.log").open("a", encoding="utf-8") as diagnostic:
                diagnostic.write(f"\n模型真实自检失败：{type(exc).__name__}: {exc}\n")
            prompt = QMessageBox(
                QMessageBox.Icon.Warning, "语音引擎尚未准备完成", str(exc)[:1500]
            )
            repair = prompt.addButton(
                "自动修复并重试", QMessageBox.ButtonRole.AcceptRole
            )
            logs = prompt.addButton("查看诊断日志", QMessageBox.ButtonRole.ActionRole)
            prompt.addButton("稍后继续", QMessageBox.ButtonRole.RejectRole)
            prompt.exec()
            if prompt.clickedButton() is logs:
                QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(data_dir / "setup.log"))
                )
                return 1
            if prompt.clickedButton() is not repair:
                return 1
            args.setup = True
            valid = False


if __name__ == "__main__":
    raise SystemExit(main())
