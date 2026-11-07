from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import requests
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSignalBlocker,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
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
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .http_errors import check_response
from .output_store import LAST_AUDIO_LIMIT, write_wav_atomic
from .preset_dialog import PresetDialog
from .qt_audio_choices import fill_local_devices, style_combo_popup
from .third_party import show_third_party


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
        except Exception as exc:  # noqa: BLE001 - surface all worker failures on the GUI thread
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
        self._workers: set[_Worker] = set()
        self._closing = False
        self._last_pcm: bytes | None = None
        self._stop_requested = False
        self._presets: dict[str, dict] = {}
        self._output_devices: list[dict] = []
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
            QLabel#cardHint { color: #5d6c84; font-size: 11px; }
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
            QPushButton#primary:disabled { color: #65718a; background: #d8dff1; }
            QPushButton#danger { color: #a3424c; }
            QPushButton#danger:disabled { color: #8793a8; background: #f1f3f7; }
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
        self.third_party_button = QPushButton("第三方许可")
        self.third_party_button.setObjectName("thirdPartyLicenses")
        self.third_party_button.clicked.connect(lambda: show_third_party(self))
        header_layout.addWidget(self.third_party_button)
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
            "角色台词", "先导入或选择角色，再输入文字；生成需要等待，并非实时变声。"
        )
        self.text = QTextEdit()
        self.text.textChanged.connect(self._update_generation_availability)
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
        self.preset.currentIndexChanged.connect(self._preset_changed)
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
        self.reference.currentTextChanged.connect(self._update_generation_availability)
        self.reference.setPlaceholderText("可选：使用自定义参考音频")
        self.reference.lineEdit().setPlaceholderText("临时参考音频（不保存为角色）")
        self.reference.lineEdit().textEdited.connect(self._temporary_reference_edited)
        character_grid.addWidget(self.reference, 1, 1, 1, 2)
        browse = QPushButton("临时使用…")
        browse.clicked.connect(self._browse_audio)
        character_grid.addWidget(browse, 1, 3)
        character_layout.addLayout(character_grid)
        role_actions = QHBoxLayout()
        self.import_preset_button = QPushButton("＋ 导入角色音频")
        self.import_preset_button.setObjectName("primary")
        self.import_preset_button.clicked.connect(lambda: self._edit_preset(False))
        role_actions.addWidget(self.import_preset_button)
        self.manage_preset_button = QPushButton("管理当前角色…")
        self.manage_preset_button.setEnabled(False)
        self.manage_preset_button.clicked.connect(lambda: self._edit_preset(True))
        role_actions.addWidget(self.manage_preset_button)
        role_actions.addStretch(1)
        character_layout.addLayout(role_actions)
        self.preset_hint = QLabel("还没有角色？点击“导入角色音频”，选择自己的参考人声并命名保存。")
        self.preset_hint.setObjectName("fieldLabel")
        self.preset_hint.setWordWrap(True)
        character_layout.addWidget(self.preset_hint)
        character_note = QLabel("软件不附带人物素材。保存的角色与临时参考音频二选一，切换角色会清除临时覆盖。")
        character_note.setObjectName("cardHint")
        character_note.setWordWrap(True)
        character_layout.addWidget(character_note)
        # Make the missing first-use action visible before the large text box.
        left.insertWidget(0, character_card)

        right = QVBoxLayout()
        right.setSpacing(15)
        content.addLayout(right, 2)

        route_card, route_layout = self._card(
            "音频路由", "接收软件的麦克风需选 CABLE Output；TS3 已有本机验证，QQ 兼容性仍待确认。"
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
        self.monitor_enabled = QCheckBox("本地播放（耳机／扬声器）")
        route_layout.addWidget(self.monitor_enabled)
        self.monitor_device = QComboBox()
        self.monitor_device.setPlaceholderText("选择耳机或扬声器")
        route_layout.addWidget(self.monitor_device)
        self.apply_mixer_button = QPushButton("应用混音设置")
        self.apply_mixer_button.setObjectName("primary")
        self.apply_mixer_button.clicked.connect(self.apply_mixer)
        route_layout.addWidget(self.apply_mixer_button)
        self.output_device.currentIndexChanged.connect(self._output_selection_changed)
        self.microphone_enabled.toggled.connect(self._update_route_hint)
        self._update_route_hint()
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
        for combo in self.findChildren(QComboBox):
            style_combo_popup(combo)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(7)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.currentTextChanged.connect(combo.setToolTip)
        self._update_generation_availability()

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
        # Keep signal objects alive until the GUI consumes the queued result.
        self._workers.add(worker)
        if on_error is None:

            def on_error(message):
                self._set_status(f"操作失败：{message}", "error")

        def done(result):
            try:
                if not self._closing and on_done:
                    on_done(result)
            finally:
                self._workers.discard(worker)

        def failed(message):
            try:
                if not self._closing:
                    on_error(message)
            finally:
                self._workers.discard(worker)

        worker.signals.result.connect(done, Qt.ConnectionType.QueuedConnection)
        worker.signals.error.connect(failed, Qt.ConnectionType.QueuedConnection)
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
        self._set_presets(data["presets"], settings.get("last_preset", ""))
        self._output_devices = data["outputs"]

        self.output_device.clear()
        self.output_device.addItem("仅本地试听（不输出到虚拟麦克风）", None)
        cable_outputs = [device for device in data["outputs"] if device.get("is_vb_cable")]
        for device in cable_outputs:
            marker = (
                "  · VB-CABLE 推荐"
                if device.get("host_api") == "MME"
                else ""
            )
            self.output_device.addItem(
                f"{device['name']}  [{device['host_api']}]{marker}", device["id"]
            )
        self._select_combo(self.output_device, settings.get("output_device"))
        has_cable = bool(cable_outputs)

        fill_local_devices(self.monitor_device, data["outputs"], settings.get("monitor_device"))
        fill_local_devices(self.microphone_device, data["inputs"], settings.get("microphone_device"))

        self.reference.setEditText(settings.get("last_reference_audio", ""))
        if self.reference.currentText().strip():
            self.preset.setCurrentIndex(0)
            self._update_preset_hint()
        self.language.setCurrentText(settings.get("language", "ZH"))
        self.monitor_enabled.setChecked(bool(settings.get("monitor_enabled", True)))
        self.microphone_enabled.setChecked(
            bool(settings.get("microphone_enabled", False))
        )
        self.microphone_enabled.setEnabled(has_cable)
        self._output_selection_changed()
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
            "模型已就绪，请先选择角色并输入台词" if self.model_ready else "界面已连接，正在加载模型…"
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
        self._update_generation_availability()
        self._set_status(
            "模型已就绪，请选择角色并输入台词" if self.model_ready else "模型尚未就绪",
            "success" if self.model_ready else "error",
        )

    def _model_error(self, message):
        self.model_ready = False
        self.generate_button.setEnabled(False)
        self._set_status(f"模型未就绪：{message}。重新启动可自动修复。", "error")

    def _output_selection_changed(self):
        if self.output_device.currentData() is None:
            self.microphone_enabled.setChecked(False)
            self.monitor_enabled.setChecked(True)
        self._update_route_hint()

    def _update_route_hint(self):
        if self.output_device.currentData() is None:
            message = "角色语音 → 本地耳机或扬声器，不送入虚拟麦克风。"
        elif self.microphone_enabled.isChecked():
            message = "真人麦克风 + 角色语音 → CABLE Output"
        else:
            message = "角色语音 → CABLE Output（虚拟麦克风）"
        self.route_text.setText(message)

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
        if preset:
            reference = None
            if not self._presets.get(preset, {}).get("reference_exists", True):
                raise ValueError("该角色的参考音频缺失，请点击“管理当前角色”替换音频。")
        if not preset and not reference:
            raise ValueError("还没有选择声音：请点击“导入角色音频”创建角色，或临时选择参考音频。")
        output_device = self.output_device.currentData()
        if output_device is None and not self.monitor_enabled.isChecked():
            raise ValueError("请开启本地播放，或选择 VB-CABLE 输出。")
        if (
            self.monitor_enabled.isChecked()
            and self.monitor_device.currentIndex() < 0
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
        if self.microphone_enabled.isChecked() and self.microphone_device.currentIndex() < 0:
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
        settings = dict(self._settings)
        settings.update(
            output_device=mixer["output_device"],
            microphone_enabled=mixer["enabled"],
            microphone_device=mixer["input_device"],
            microphone_gain=mixer["mic_gain"],
            tts_gain=mixer["tts_gain"],
            monitor_enabled=self.monitor_enabled.isChecked(),
            monitor_device=self.monitor_device.currentData(),
        )

        def task():
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
        self.import_preset_button.setEnabled(False)
        self.manage_preset_button.setEnabled(False)
        self.save_output.setEnabled(False)
        self.output_limit.setEnabled(False)
        self.save_storage_button.setEnabled(False)
        self._stop_requested = False
        self._last_pcm = None
        self.save_last_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_status(
            "正在生成，将播放到耳机或扬声器…"
            if payload["output_device"] is None
            else "正在生成，将输出到 VB-CABLE…"
        )

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
            if job_id:
                try:
                    saved = self._get(f"/v1/outputs/{job_id}")
                except Exception as exc:  # noqa: BLE001 - result lookup must not discard generated audio
                    saved = {"saved": False, "playback_unknown": str(exc)}
                    if payload["save_output"]:
                        saved["error"] = "未能确认自动保存结果。"
            return {"pcm": bytes(audio) if audio else None, "too_large": too_large,
                    "saved": saved}

        self._run_async(task, self._generation_done, self._generation_error)

    def _generation_done(self, result):
        self.generating = False
        self.import_preset_button.setEnabled(True)
        self._update_preset_hint()
        self.save_output.setEnabled(True)
        self.output_limit.setEnabled(self.save_output.isChecked())
        self.save_storage_button.setEnabled(True)
        self._last_pcm = result["pcm"]
        self.save_last_button.setEnabled(self._last_pcm is not None)
        self._update_generation_availability()
        self.stop_button.setEnabled(False)
        prefix = "已停止" if self._stop_requested else "生成完成"
        saved = result.get("saved") or {}
        state = "success"
        storage_label = "未自动保存"
        if saved.get("saved"):
            storage_label = "已自动保存"
            detail = f"自动保存位置：{saved['path']}。超过保留上限时会清理最早的自动保存音频。"
        elif saved.get("error"):
            state = ""
            detail = saved["error"] + "；可尝试另存上一条。"
        else:
            detail = "本条没有写入硬盘；需要保留时点击“另存上一条”。"
            if result.get("too_large"):
                detail = "本条超过 64 MB 临时内存上限，未暂存；长音频需要提前开启自动保存。"
        if saved.get("playback_error"):
            self._set_status(
                f"{prefix} · 播放异常", "error",
                f"{saved['playback_error']} {detail}",
            )
        elif saved.get("playback_unknown"):
            self._set_status(f"{prefix} · 播放状态未确认", "", detail)
        else:
            self._set_status(f"{prefix} · {storage_label}", state, detail)

    def _generation_error(self, message: str):
        self.generating = False
        self.import_preset_button.setEnabled(True)
        self._update_preset_hint()
        self.save_output.setEnabled(True)
        self.output_limit.setEnabled(self.save_output.isChecked())
        self.save_storage_button.setEnabled(True)
        self._update_generation_availability()
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
            self.preset.setCurrentIndex(0)
            self.reference.setEditText(path)
            self._update_preset_hint()

    def _set_presets(self, items, selected=""):
        self._presets = {item["name"]: item for item in items}
        blocker = QSignalBlocker(self.preset)
        self.preset.clear()
        self.preset.addItem("（临时音频 / 尚未选择角色）", "")
        for item in items:
            label = item["name"] + (" · 音频缺失" if not item.get("reference_exists", True) else "")
            self.preset.addItem(label, item["name"])
        self._select_combo(self.preset, selected)
        del blocker
        self._preset_changed()

    def _preset_changed(self, *_args):
        selected = self.preset.currentData()
        if selected and hasattr(self, "reference"):
            self.reference.setEditText("")
            item = self._presets.get(selected, {})
            if item.get("language"):
                self.language.setCurrentText(item["language"])
        self._update_preset_hint()

    def _temporary_reference_edited(self, text):
        if text.strip():
            self.preset.setCurrentIndex(0)
        self._update_preset_hint()

    def _update_preset_hint(self):
        if not hasattr(self, "preset_hint"):
            return
        selected = self.preset.currentData()
        self.manage_preset_button.setEnabled(bool(selected) and not self.generating)
        if selected:
            item = self._presets.get(selected, {})
            if not item.get("reference_exists", True):
                text = f"“{selected}”的参考音频已缺失，请点击“管理当前角色”替换。"
            else:
                seconds = item.get("duration_seconds")
                duration = f" · {seconds:.1f} 秒" if isinstance(seconds, (int, float)) else ""
                text = f"正在使用保存的角色：{selected}{duration}。可管理、试听或替换参考音频。"
        elif self.reference.currentText().strip():
            text = "正在临时使用参考音频；如需以后反复选择，请通过“导入角色音频”保存角色。"
        else:
            text = "首次使用：① 导入角色音频并保存  ② 输入台词  ③ 生成并输出。"
        self.preset_hint.setText(text)
        self._update_generation_availability()

    def _update_generation_availability(self, *_args):
        if not hasattr(self, "generate_button"):
            return
        name = self.preset.currentData()
        valid_role = bool(name) and self._presets.get(name, {}).get("reference_exists", True)
        has_source = valid_role or (not name and bool(self.reference.currentText().strip()))
        has_text = bool(self.text.toPlainText().strip())
        self.generate_button.setEnabled(self.model_ready and not self.generating and bool(has_source) and has_text)
        self.generate_button.setToolTip("先导入/选择可用角色，并输入台词；模型就绪后即可生成。")

    def _edit_preset(self, editing):
        if self.generating:
            self._set_status("请先完成或停止生成，再管理角色。", "error")
            return
        item = self._presets.get(self.preset.currentData()) if editing else None
        if editing and item is None:
            self._set_status("请先选择要管理的角色。", "error")
            return
        dialog = PresetDialog(self.base_url, item, self._output_devices,
                              self.monitor_device.currentData(), self)
        if not editing and self.reference.currentText().strip():
            dialog.source_path = Path(self.reference.currentText().strip())
            dialog.file.setText(str(dialog.source_path))
            dialog.name.setText(dialog.source_path.stem[:60])
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.result_name or ""
        def task():
            items = self._get("/v1/presets")
            settings = self._get("/v1/settings")
            settings.update(last_preset=selected, last_reference_audio="")
            self._put("/v1/settings", settings)
            return items
        def done(items):
            self.reference.setEditText("")
            self._set_presets(items, selected)
            self._settings.update(last_preset=selected, last_reference_audio="")
            self._set_status("角色已保存并选中，可输入台词生成。" if selected else "角色已移入回收区。", "success")
        self._run_async(task, done)

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
        self._closing = True
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
