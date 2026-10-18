from __future__ import annotations

from pathlib import Path
import time
from typing import Callable

import requests

from . import __version__
from .http_errors import check_response
from .output_store import LAST_AUDIO_LIMIT, write_wav_atomic
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class _WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, callback: Callable[[], object]):
        super().__init__()
        self.callback = callback
        self.signals = _WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.result.emit(self.callback())
        except Exception as exc:  # surfaced on the GUI thread
            try:
                self.signals.error.emit(str(exc))
            except RuntimeError:
                # The window may have been closed while a network worker was finishing.
                pass


class VoiceDesktopQt(QMainWindow):
    """Modern Qt desktop client for the existing local Voice App API."""

    def __init__(
        self,
        base_url: str,
        on_close: Callable[[], None] | None = None,
        model_ready: bool = False,
    ):
        self.qt_app = QApplication.instance()
        if self.qt_app is None:
            self.qt_app = QApplication([])
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.on_close_callback = on_close
        self.pool = QThreadPool.globalInstance()
        self.generating = False
        self.model_ready = model_ready
        self._settings: dict = {}
        self._last_pcm: bytes | None = None
        self._stop_requested = False
        self.setWindowTitle(f"IndexTTS Voice App v{__version__}")
        self.setMinimumSize(980, 760)
        self.resize(1160, 860)
        self._set_style()
        self._build_ui()
        self._refresh_data()

    def _set_style(self):
        self.qt_app.setStyle("Fusion")
        self.setStyleSheet(
            """
            QMainWindow, QWidget#root { background: #f3f5fa; }
            QFrame#headerCard {
                background: #202b47;
                border: none;
                border-radius: 22px;
            }
            QFrame#card {
                background: #ffffff;
                border: 1px solid #e2e7f0;
                border-radius: 18px;
            }
            QLabel#title {
                color: #ffffff;
                font-size: 25px;
                font-weight: 700;
            }
            QLabel#subtitle { color: #cbd5e8; font-size: 11px; }
            QLabel#cardTitle {
                color: #26334f;
                font-size: 14px;
                font-weight: 700;
            }
            QLabel#cardHint { color: #77839a; font-size: 11px; }
            QLabel#fieldLabel { color: #53617a; font-size: 11px; font-weight: 600; }
            QLabel#routeText {
                color: #4b5d7e;
                background: #eef2fb;
                border: 1px solid #dce5fb;
                border-radius: 12px;
                padding: 9px;
                min-height: 30px;
                font-size: 12px;
            }
            QLabel#statusPill {
                color: #33466f;
                background: #e9eef8;
                border-radius: 12px;
                padding: 8px 12px;
                font-size: 11px;
            }
            QLabel#statusPill[state="success"] {
                color: #176a48;
                background: #e3f6ed;
            }
            QLabel#statusPill[state="error"] {
                color: #9a3841;
                background: #fdebed;
            }
            QTextEdit, QComboBox, QLineEdit {
                color: #26334f;
                background: #fbfcff;
                border: 1px solid #d9e0ec;
                border-radius: 11px;
                padding: 8px 10px;
                selection-background-color: #526edb;
            }
            QTextEdit:focus, QComboBox:focus, QLineEdit:focus {
                border: 1px solid #6883e5;
            }
            QComboBox { min-height: 23px; }
            QComboBox::drop-down {
                width: 30px;
                border: none;
            }
            QCheckBox { color: #34415d; spacing: 8px; }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border-radius: 5px;
                border: 1px solid #c4cede;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #526edb;
                border: 1px solid #526edb;
            }
            QSlider::groove:horizontal {
                height: 5px;
                background: #e2e7f0;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #6883e5;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 16px; margin: -6px 0;
                border-radius: 8px;
                background: #526edb;
            }
            QPushButton {
                color: #40506f;
                background: #ffffff;
                border: 1px solid #d4dceb;
                border-radius: 11px;
                padding: 9px 15px;
                font-size: 11px;
            }
            QPushButton:hover { background: #f1f4fb; }
            QPushButton:disabled { color: #a1aabd; background: #f1f3f7; }
            QPushButton#primary {
                color: #ffffff;
                background: #526edb;
                border: none;
                font-weight: 700;
                padding: 10px 20px;
            }
            QPushButton#primary:hover { background: #425dca; }
            QPushButton#danger { color: #a3424c; }
            QToolTip {
                color: #ffffff;
                background: #26334f;
                border: none;
                padding: 5px;
            }
            """
        )

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(15)

        header = QFrame()
        header.setObjectName("headerCard")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 16)
        header_layout.setSpacing(14)
        logo = QLabel("V")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(42, 42)
        logo.setStyleSheet(
            "color: #ffffff; background: #526edb; border-radius: 14px; font-size: 23px;"
        )
        header_layout.addWidget(logo)
        title_box = QVBoxLayout()
        title = QLabel("IndexTTS Voice App")
        title.setObjectName("title")
        subtitle = QLabel("本地角色语音 · 实时混音 · 虚拟麦克风")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        self.status_pill = QLabel("正在连接本地服务…")
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setMaximumWidth(330)
        self.status_pill.setMinimumWidth(190)
        self.status_pill.setWordWrap(True)
        header_layout.addWidget(self.status_pill)
        outer.addWidget(header)

        self.workspace_scroll = QScrollArea()
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.workspace_scroll.setStyleSheet("QScrollArea { background: #f3f5fa; border: none; }")
        workspace = QWidget()
        workspace.setObjectName("root")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(15)
        self.workspace_scroll.setWidget(workspace)
        outer.addWidget(self.workspace_scroll, 1)
        content = QHBoxLayout()
        content.setSpacing(15)
        workspace_layout.addLayout(content, 1)

        left = QVBoxLayout()
        left.setSpacing(15)
        content.addLayout(left, 3)

        text_card, text_layout = self._card(
            "角色台词", "输入文字，生成的声音会立即进入虚拟麦克风。"
        )
        self.text = QTextEdit()
        self.text.setPlaceholderText("例如：大家好，今天也要一起加油哦！")
        self.text.setMinimumHeight(210)
        self.text.setFont(QFont("Microsoft YaHei UI", 11))
        text_layout.addWidget(self.text, 1)
        storage_row = QHBoxLayout()
        storage_row.setSpacing(7)
        self.save_output = QCheckBox("自动保存音频")
        self.save_output.setToolTip("默认关闭；开启后写入专用目录，并按容量上限清理最早的自动保存音频。")
        storage_row.addWidget(self.save_output)
        storage_row.addWidget(self._label("上限"))
        self.output_limit = QComboBox()
        for label, value in (("100 MB", 100), ("500 MB", 500), ("1 GB", 1024), ("2 GB", 2048)):
            self.output_limit.addItem(label, value)
        self._select_combo(self.output_limit, 500)
        self.output_limit.setEnabled(False)
        self.save_output.toggled.connect(self.output_limit.setEnabled)
        storage_row.addWidget(self.output_limit)
        self.save_storage_button = QPushButton("记住选项")
        self.save_storage_button.clicked.connect(self.save_storage_preferences)
        storage_row.addWidget(self.save_storage_button)
        self.save_last_button = QPushButton("另存上一条…")
        self.save_last_button.setEnabled(False)
        self.save_last_button.setToolTip("只暂存最近一条（最多 64 MB）；关闭窗口或再次生成会丢弃，手动另存不参与自动清理。")
        self.save_last_button.clicked.connect(self.save_last_audio)
        storage_row.addWidget(self.save_last_button)
        text_layout.addLayout(storage_row)
        storage_hint = QLabel("默认不写硬盘；超限清理最早的自动保存音频，手动另存不参与清理。")
        storage_hint.setObjectName("cardHint")
        storage_hint.setWordWrap(True)
        text_layout.addWidget(storage_hint)
        left.addWidget(text_card, 1)

        character_card, character_layout = self._card("角色与语言")
        character_grid = QGridLayout()
        character_grid.setHorizontalSpacing(12)
        character_grid.setVerticalSpacing(9)
        character_grid.setColumnStretch(1, 1)
        character_grid.setColumnStretch(3, 1)
        character_grid.addWidget(self._label("角色预设"), 0, 0)
        self.preset = QComboBox()
        self.preset.setPlaceholderText("选择一个角色")
        character_grid.addWidget(self.preset, 0, 1)
        character_grid.addWidget(self._label("语言"), 0, 2)
        self.language = QComboBox()
        self.language.addItems(["ZH", "EN", "JA", "ES"])
        self.language.setCurrentText("ZH")
        character_grid.addWidget(self.language, 0, 3)
        character_grid.addWidget(self._label("参考音频"), 1, 0)
        self.reference = QComboBox()
        self.reference.setEditable(True)
        self.reference.setPlaceholderText("可选：使用自定义参考音频")
        character_grid.addWidget(self.reference, 1, 1, 1, 2)
        browse = QPushButton("浏览文件")
        browse.clicked.connect(self._browse_audio)
        character_grid.addWidget(browse, 1, 3)
        character_layout.addLayout(character_grid)
        left.addWidget(character_card)

        right = QVBoxLayout()
        right.setSpacing(15)
        content.addLayout(right, 2)

        route_card, route_layout = self._card(
            "音频路由", "把两路声音合成一条，TeamSpeak 只需要选择 CABLE Output。"
        )
        route_layout.addWidget(self._label("真人麦克风"))
        self.microphone_device = QComboBox()
        self.microphone_device.setPlaceholderText("选择你的真实麦克风")
        route_layout.addWidget(self.microphone_device)
        route_layout.addSpacing(5)
        route_layout.addWidget(self._label("虚拟输出（CABLE Input）"))
        self.output_device = QComboBox()
        self.output_device.setPlaceholderText("选择 VB-CABLE 推荐设备")
        route_layout.addWidget(self.output_device)
        self.microphone_enabled = QCheckBox("开启真人麦克风透传")
        self.microphone_enabled.setToolTip("把真实麦克风持续混入 CABLE Output")
        route_layout.addWidget(self.microphone_enabled)
        self.route_text = QLabel("真人麦克风   +   角色语音   →   CABLE Output")
        self.route_text.setObjectName("routeText")
        self.route_text.setWordWrap(True)
        route_layout.addWidget(self.route_text)

        mix_grid = QGridLayout()
        mix_grid.setHorizontalSpacing(10)
        mix_grid.addWidget(self._label("真人音量"), 0, 0)
        self.microphone_gain = QSlider(Qt.Orientation.Horizontal)
        self.microphone_gain.setRange(0, 200)
        self.microphone_gain.setValue(100)
        self.microphone_gain.valueChanged.connect(self._update_gain_labels)
        mix_grid.addWidget(self.microphone_gain, 0, 1)
        self.mic_gain_label = QLabel("100%")
        mix_grid.addWidget(self.mic_gain_label, 0, 2)
        mix_grid.addWidget(self._label("角色音量"), 1, 0)
        self.tts_gain = QSlider(Qt.Orientation.Horizontal)
        self.tts_gain.setRange(0, 200)
        self.tts_gain.setValue(100)
        self.tts_gain.valueChanged.connect(self._update_gain_labels)
        mix_grid.addWidget(self.tts_gain, 1, 1)
        self.tts_gain_label = QLabel("100%")
        mix_grid.addWidget(self.tts_gain_label, 1, 2)
        route_layout.addLayout(mix_grid)
        route_layout.addSpacing(4)
        self.monitor_enabled = QCheckBox("同时监听到耳机")
        route_layout.addWidget(self.monitor_enabled)
        self.monitor_device = QComboBox()
        self.monitor_device.setPlaceholderText("选择耳机或扬声器")
        route_layout.addWidget(self.monitor_device)
        self.apply_mixer_button = QPushButton("应用混音设置")
        self.apply_mixer_button.setObjectName("primary")
        self.apply_mixer_button.clicked.connect(self.apply_mixer)
        route_layout.addWidget(self.apply_mixer_button)
        route_layout.addStretch(1)
        right.addWidget(route_card, 1)

        advanced, advanced_layout = self._card(
            "生成参数", "通常保持默认即可，需要细调音色时再修改。"
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.top_p = self._entry("0.8")
        self.top_k = self._entry("30")
        self.temperature = self._entry("0.8")
        self.segment_tokens = self._entry("120")
        self.duration_factor = self._entry("1.0")
        for column, label, entry in (
            (0, "Top P", self.top_p),
            (2, "Top K", self.top_k),
            (4, "温度", self.temperature),
            (6, "分段长度", self.segment_tokens),
            (8, "语速系数", self.duration_factor),
        ):
            grid.addWidget(self._label(label), 0, column)
            grid.addWidget(entry, 0, column + 1)
        for column in (1, 3, 5, 7, 9):
            grid.setColumnStretch(column, 1)
        advanced_layout.addLayout(grid)
        workspace_layout.addWidget(advanced)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.status_detail = QLabel("准备连接…")
        self.status_detail.setObjectName("cardHint")
        self.status_detail.setWordWrap(True)
        self.status_detail.setMaximumHeight(64)
        footer.addWidget(self.status_detail, 1)
        self.refresh_button = QPushButton("刷新设备")
        self.refresh_button.clicked.connect(self._refresh_data)
        footer.addWidget(self.refresh_button)
        self.stop_button = QPushButton("停止生成")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        footer.addWidget(self.stop_button)
        self.generate_button = QPushButton("生成并输出")
        self.generate_button.setObjectName("primary")
        self.generate_button.setEnabled(self.model_ready)
        self.generate_button.clicked.connect(self.generate)
        footer.addWidget(self.generate_button)
        outer.addLayout(footer)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    @staticmethod
    def _entry(value: str):
        from PySide6.QtWidgets import QLineEdit

        entry = QLineEdit(value)
        entry.setMaximumWidth(90)
        return entry

    @staticmethod
    def _card(title: str, hint: str | None = None):
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 15, 17, 15)
        layout.setSpacing(9)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        layout.addWidget(title_label)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("cardHint")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)
        return card, layout

    def _update_gain_labels(self):
        self.mic_gain_label.setText(f"{self.microphone_gain.value()}%")
        self.tts_gain_label.setText(f"{self.tts_gain.value()}%")

    def _run_async(self, callback, on_done=None, on_error=None):
        worker = _Worker(callback)
        if on_done:
            worker.signals.result.connect(on_done)
        if on_error is None:

            def on_error(message):
                self._set_status(f"操作失败：{message}", "error")

        worker.signals.error.connect(on_error)
        self.pool.start(worker)

    def _refresh_data(self):
        self._set_status("正在读取设备和角色…")

        def request():
            return {
                "presets": self._get("/v1/presets"),
                "outputs": self._get("/v1/devices"),
                "inputs": self._get("/v1/input-devices"),
                "settings": self._get("/v1/settings"),
            }

        self._run_async(request, self._apply_data)

    def _apply_data(self, data: dict):
        settings = data["settings"]
        self._settings = settings
        self.save_output.setChecked(bool(settings.get("save_output", False)))
        limit = int(settings.get("output_limit_mb", 500))
        if self.output_limit.findData(limit) < 0:
            self.output_limit.addItem(f"{limit} MB", limit)
        self._select_combo(self.output_limit, limit)
        self.preset.clear()
        self.preset.addItem("（不使用预设）", "")
        for item in data["presets"]:
            self.preset.addItem(item["name"], item["name"])
        self._select_combo(self.preset, settings.get("last_preset", ""))

        self.output_device.clear()
        self.output_device.addItem("仅本地试听（不输出到虚拟麦克风）", None)
        for device in data["outputs"]:
            marker = (
                "  · VB-CABLE 推荐"
                if (device.get("is_vb_cable") and device.get("host_api") == "MME")
                else ("  · VB-CABLE" if device.get("is_vb_cable") else "")
            )
            self.output_device.addItem(
                f"{device['name']}  [{device['host_api']}]{marker}", device["id"]
            )
        self._select_output(data["outputs"], settings.get("output_device"))
        has_cable = any(device.get("is_vb_cable") for device in data["outputs"])
        if not has_cable:
            self.route_text.setText(
                "未检测到 VB-CABLE，当前可使用耳机/扬声器本地试听。"
                "虚拟麦克风可在安装向导中安装官方驱动；安装后可能需要重启。"
            )
        else:
            self.route_text.setText("真人麦克风   +   角色语音   →   CABLE Output")

        self.monitor_device.clear()
        for device in data["outputs"]:
            self.monitor_device.addItem(
                f"{device['name']}  [{device['host_api']}]",
                device["id"],
            )
        self._select_combo(self.monitor_device, settings.get("monitor_device"))

        self.microphone_device.clear()
        inputs = [item for item in data["inputs"] if not item.get("is_vb_cable")]
        for device in inputs:
            self.microphone_device.addItem(
                f"{device['name']}  [{device['host_api']}]", device["id"]
            )
        self._select_input(inputs, settings.get("microphone_device"))

        self.reference.setEditText(settings.get("last_reference_audio", ""))
        self.language.setCurrentText(settings.get("language", "ZH"))
        self.monitor_enabled.setChecked(bool(settings.get("monitor_enabled", False)))
        self.microphone_enabled.setChecked(
            bool(settings.get("microphone_enabled", False))
        )
        self.microphone_enabled.setEnabled(has_cable)
        if not has_cable:
            self.microphone_enabled.setChecked(False)
            self.monitor_enabled.setChecked(True)
            local_outputs = [d for d in data["outputs"] if not d.get("is_vb_cable")]
            configured = settings.get("monitor_device")
            chosen = next((d for d in local_outputs if d["id"] == configured), None)
            chosen = (
                chosen
                or next((d for d in local_outputs if d.get("is_default")), None)
                or (local_outputs[0] if local_outputs else None)
            )
            if chosen:
                self._select_combo(self.monitor_device, chosen["id"])
        self.microphone_gain.setValue(
            round(float(settings.get("microphone_gain", 1)) * 100)
        )
        self.tts_gain.setValue(round(float(settings.get("tts_gain", 1)) * 100))
        self.top_p.setText(str(settings.get("top_p", 0.8)))
        self.top_k.setText(str(settings.get("top_k", 30)))
        self.temperature.setText(str(settings.get("temperature", 0.8)))
        self.segment_tokens.setText(
            str(settings.get("max_text_tokens_per_segment", 120))
        )
        self.duration_factor.setText(str(settings.get("duration_factor", 1.0)))
        self._set_status(
            "模型已就绪，可以生成" if self.model_ready else "界面已连接，正在加载模型…"
        )
        if self.microphone_enabled.isChecked():
            try:
                mixer = self._mixer_payload()
            except ValueError as exc:
                self._set_status(f"麦克风透传未启动：{exc}", "error")
            else:
                self._run_async(
                    lambda: self._configure_mixer(mixer),
                    lambda _result: self._set_status(
                        "麦克风透传已恢复，目标软件继续使用 CABLE Output", "success"
                    ),
                    lambda message: self._set_status(
                        f"麦克风透传启动失败：{message}", "error"
                    ),
                )
        if not self.model_ready:
            self._run_async(
                lambda: self._post("/v1/model/load"),
                self._model_loaded,
                self._model_error,
            )

    def _model_loaded(self, result):
        self.model_ready = bool(result.get("model_loaded"))
        self.generate_button.setEnabled(self.model_ready and not self.generating)
        self._set_status(
            "模型已就绪，可以生成" if self.model_ready else "模型尚未就绪",
            "success" if self.model_ready else "error",
        )

    def _model_error(self, message):
        self.model_ready = False
        self.generate_button.setEnabled(False)
        self._set_status(f"模型未就绪：{message}。重新启动可自动修复。", "error")

    def _select_output(self, devices: list[dict], configured):
        preferred = next(
            (
                d
                for d in devices
                if d.get("is_vb_cable")
                and d.get("host_api") == "MME"
                and int(d.get("default_samplerate", 0)) == 44100
            ),
            None,
        )
        chosen = next((d for d in devices if d["id"] == configured), None)
        if not chosen or not chosen.get("is_vb_cable"):
            chosen = preferred or next(
                (d for d in devices if d.get("is_vb_cable")), None
            )
        if chosen:
            self._select_combo(self.output_device, chosen["id"])

    def _select_input(self, devices: list[dict], configured):
        chosen = next((d for d in devices if d["id"] == configured), None)
        preferred = [
            d
            for d in devices
            if d.get("host_api") == "MME"
            and not any(
                word in d.get("name", "").casefold()
                for word in ("virtual", "ivcam", "todesk", "netease", "网易")
            )
        ]
        if chosen is None:
            chosen = next(
                (
                    d
                    for d in preferred
                    if "麦克风" in d.get("name", "")
                    or "microphone" in d.get("name", "").casefold()
                ),
                None,
            ) or (preferred[0] if preferred else (devices[0] if devices else None))
        if chosen:
            self._select_combo(self.microphone_device, chosen["id"])

    @staticmethod
    def _select_combo(combo: QComboBox, value):
        if value is None:
            return
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _payload(self) -> dict:
        text = self.text.toPlainText().strip()
        if not text:
            raise ValueError("请先输入角色台词。")
        preset = self.preset.currentData() or None
        reference = self.reference.currentText().strip() or None
        if not preset and not reference:
            raise ValueError("请选择角色预设或参考音频。")
        output_device = self.output_device.currentData()
        if output_device is None and not self.monitor_enabled.isChecked():
            raise ValueError("本地试听需要勾选耳机监听；或选择 VB-CABLE 输出。")
        if (
            self.monitor_enabled.isChecked()
            and self.monitor_device.currentData() is None
        ):
            raise ValueError("没有可用的耳机或扬声器，请连接后刷新设备。")
        return {
            "text": text,
            "preset": preset,
            "reference_audio": reference,
            "language": self.language.currentText() or "ZH",
            "output_device": int(output_device) if output_device is not None else None,
            "monitor_enabled": self.monitor_enabled.isChecked(),
            "monitor_device": self.monitor_device.currentData(),
            "save_output": self.save_output.isChecked(),
            "generation": {
                "top_p": float(self.top_p.text()),
                "top_k": int(self.top_k.text()),
                "temperature": float(self.temperature.text()),
                "max_text_tokens_per_segment": int(self.segment_tokens.text()),
                "duration_factor": float(self.duration_factor.text()),
            },
        }

    def _mixer_payload(self) -> dict:
        output_device = self.output_device.currentData()
        if output_device is None and self.microphone_enabled.isChecked():
            raise ValueError("请选择 VB-CABLE 输出设备。")
        input_device = self.microphone_device.currentData()
        if self.microphone_enabled.isChecked() and input_device is None:
            raise ValueError("请选择真实麦克风。")
        return {
            "enabled": self.microphone_enabled.isChecked(),
            "input_device": input_device,
            "output_device": int(output_device) if output_device is not None else None,
            "mic_gain": self.microphone_gain.value() / 100,
            "tts_gain": self.tts_gain.value() / 100,
        }

    def _settings_payload(self, payload: dict, mixer: dict) -> dict:
        value = dict(self._settings)
        generation = payload["generation"]
        value.update(
            {
                "output_device": payload["output_device"],
                "microphone_enabled": mixer["enabled"],
                "microphone_device": mixer["input_device"],
                "microphone_gain": mixer["mic_gain"],
                "tts_gain": mixer["tts_gain"],
                "monitor_enabled": payload["monitor_enabled"],
                "monitor_device": payload["monitor_device"],
                "last_preset": payload["preset"] or "",
                "last_reference_audio": payload["reference_audio"] or "",
                **self._storage_preferences(),
                "language": payload["language"],
                **generation,
            }
        )
        return value

    def _storage_preferences(self) -> dict:
        return {"save_output": self.save_output.isChecked(),
                "output_limit_mb": self.output_limit.currentData() or 500}

    def save_storage_preferences(self):
        preferences = self._storage_preferences()
        self._settings.update(preferences)
        self.save_storage_button.setEnabled(False)

        def task():
            settings = self._get("/v1/settings")
            settings.update(preferences)
            return self._put("/v1/settings", settings)

        def done(_result):
            self.save_storage_button.setEnabled(not self.generating)
            self._set_status("保存选项已记住", "success")

        def error(message):
            self.save_storage_button.setEnabled(not self.generating)
            self._set_status(f"保存选项失败：{message}", "error")

        self._run_async(task, done, error)

    def save_last_audio(self):
        if self._last_pcm is None or self.generating:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "另存上一条生成音频",
            f"voice-{time.strftime('%Y%m%d-%H%M%S')}.wav", "WAV 音频 (*.wav)",
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".wav"
        try:
            write_wav_atomic(Path(path), self._last_pcm)
        except (OSError, ValueError) as exc:
            self._set_status(f"另存失败：{exc}", "error")
            return
        self._set_status("音频已另存", "success", f"保存位置：{path}；此文件不参与自动清理。")

    def apply_mixer(self):
        try:
            mixer = self._mixer_payload()
        except ValueError as exc:
            self._set_status(str(exc), "error")
            return
        self.apply_mixer_button.setEnabled(False)
        self._set_status("正在应用混音设置…")

        def task():
            settings = dict(self._settings)
            settings.update(
                {
                    "output_device": mixer["output_device"],
                    "microphone_enabled": mixer["enabled"],
                    "microphone_device": mixer["input_device"],
                    "microphone_gain": mixer["mic_gain"],
                    "tts_gain": mixer["tts_gain"],
                }
            )
            self._put("/v1/settings", settings)
            self._configure_mixer(mixer)

        self._run_async(
            task,
            lambda _result: self._mixer_done(True),
            lambda message: self._mixer_error(message),
        )

    def _mixer_done(self, _success: bool):
        self.apply_mixer_button.setEnabled(True)
        state = "已开启" if self.microphone_enabled.isChecked() else "已关闭"
        self._set_status(f"混音透传{state}，TeamSpeak 保持选择 CABLE Output", "success")

    def _mixer_error(self, message: str):
        self.apply_mixer_button.setEnabled(True)
        self._set_status(f"混音设置失败：{message}", "error")

    def _configure_mixer(self, mixer: dict):
        if mixer["enabled"]:
            body = {key: value for key, value in mixer.items() if key != "enabled"}
            return self._post("/v1/mixer/start", body)
        return self._post("/v1/mixer/stop")

    def generate(self):
        if self.generating:
            return
        if not self.model_ready:
            self._set_status("模型尚未就绪，请完成安装与自检后再生成。", "error")
            return
        try:
            payload = self._payload()
            mixer = self._mixer_payload()
        except (ValueError, TypeError) as exc:
            self._set_status(str(exc), "error")
            return
        self.generating = True
        self.save_output.setEnabled(False)
        self.output_limit.setEnabled(False)
        self.save_storage_button.setEnabled(False)
        self._stop_requested = False
        self._last_pcm = None
        self.save_last_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_status("正在生成，音频会实时进入虚拟麦克风…")

        def task():
            self._put("/v1/settings", self._settings_payload(payload, mixer))
            self._configure_mixer(mixer)
            audio = bytearray()
            too_large = False
            with requests.post(
                self.base_url + "/v1/tts/stream",
                json=payload,
                stream=True,
                timeout=(15, None),
            ) as response:
                check_response(response)
                job_id = response.headers.get("X-Job-Id", "")
                for chunk in response.iter_content(chunk_size=65536):
                    if not too_large:
                        if len(audio) + len(chunk) <= LAST_AUDIO_LIMIT:
                            audio.extend(chunk)
                        else:
                            audio.clear()
                            too_large = True
            saved = None
            if payload["save_output"]:
                try:
                    saved = self._get(f"/v1/outputs/{job_id}")
                except Exception as exc:
                    saved = {"saved": False, "error": f"无法确认自动保存结果：{exc}"}
            return {"pcm": bytes(audio) if audio else None, "too_large": too_large,
                    "saved": saved}

        self._run_async(task, self._generation_done, self._generation_error)

    def _generation_done(self, result):
        self.generating = False
        self.save_output.setEnabled(True)
        self.output_limit.setEnabled(self.save_output.isChecked())
        self.save_storage_button.setEnabled(True)
        self._last_pcm = result["pcm"]
        self.save_last_button.setEnabled(self._last_pcm is not None)
        self.generate_button.setEnabled(self.model_ready)
        self.stop_button.setEnabled(False)
        prefix = "已停止" if self._stop_requested else "生成完成"
        saved = result.get("saved")
        if saved and saved.get("saved"):
            detail = f"自动保存位置：{saved['path']}。超过保留上限时会清理最早的自动保存音频。"
            self._set_status(f"{prefix} · 已自动保存", "success", detail)
        elif saved and saved.get("error"):
            self._set_status(f"{prefix} · 未自动保存", "", saved["error"] + "；可尝试另存上一条。")
        else:
            detail = "本条没有写入硬盘；需要保留时点击“另存上一条”。"
            if result.get("too_large"):
                detail = "本条超过 64 MB 临时内存上限，未暂存；长音频需要提前开启自动保存。"
            self._set_status(f"{prefix} · 未自动保存", "success", detail)

    def _generation_error(self, message: str):
        self.generating = False
        self.save_output.setEnabled(True)
        self.output_limit.setEnabled(self.save_output.isChecked())
        self.save_storage_button.setEnabled(True)
        self.generate_button.setEnabled(self.model_ready)
        self.stop_button.setEnabled(False)
        self._set_status(f"生成失败：{message}", "error")

    def stop(self):
        self._stop_requested = True
        self._set_status("正在停止当前生成…")
        self._run_async(
            lambda: self._post("/v1/stop"),
            lambda _result: self._set_status("已请求停止，当前音频段结束后会停止"),
        )

    def _get(self, path: str):
        response = requests.get(self.base_url + path, timeout=10)
        check_response(response)
        return response.json()

    def _post(self, path: str, body: dict | None = None):
        response = requests.post(self.base_url + path, json=body, timeout=None)
        check_response(response)
        return response.json()

    def _put(self, path: str, body: dict):
        response = requests.put(self.base_url + path, json=body, timeout=10)
        check_response(response)
        return response.json()

    def _browse_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择参考音频",
            "",
            "音频文件 (*.wav *.mp3 *.ogg *.flac *.m4a);;所有文件 (*.*)",
        )
        if path:
            self.reference.setEditText(path)

    def _set_status(self, text: str, state: str = "", detail: str | None = None):
        self.status_pill.setText(text if len(text) <= 70 else text[:69] + "…")
        detail = detail or text
        self.status_detail.setText(detail if len(detail) <= 200 else detail[:199] + "…")
        self.status_pill.setToolTip(text)
        self.status_detail.setToolTip(detail)
        self.status_pill.setProperty("state", state)
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)

    def closeEvent(self, event):
        if self.generating:
            try:
                requests.post(self.base_url + "/v1/stop", timeout=2)
            except requests.RequestException:
                pass
        if self.on_close_callback:
            self.on_close_callback()
        event.accept()

    def run(self) -> int:
        self.show()
        return QApplication.instance().exec()


VoiceDesktop = VoiceDesktopQt
