"""Desktop role editor. Audio is sent only to the local Voice App service."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .http_errors import check_response
from .qt_audio_choices import fill_local_devices, style_combo_popup
from .reference_limits import MAX_UPLOAD_BYTES, PREVIEW_READ_TIMEOUT


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.callback())
        except Exception as exc:  # noqa: BLE001 - deliver worker failures to the UI thread
            try:
                self.signals.failed.emit(str(exc))
            except RuntimeError:
                pass


class PresetDialog(QDialog):
    def __init__(
        self, base_url, preset=None, devices=(), monitor_device=None, parent=None
    ):
        super().__init__(parent)
        self.base_url = base_url.rstrip("/")
        self.existing = preset
        self.result_name = None
        self.source_path = None
        self._busy = False
        self._previewing = False
        self._close_after_preview = False
        self._stop_pending = False
        self._workers = set()
        self.setWindowTitle("管理角色预设" if preset else "导入角色音频")
        self.resize(670, 520)
        self.setMinimumWidth(580)
        self.setStyleSheet("""
            QDialog { background: #f3f5fa; }
            QLabel, QCheckBox { color: #26334f; }
            QLineEdit, QComboBox { background: #ffffff; color: #26334f;
                border: 1px solid #cbd5e1; border-radius: 7px; padding: 8px; }
            QPushButton { background: white; color: #34466c; border: 1px solid #cbd5e1;
                border-radius: 7px; padding: 9px 14px; }
            QPushButton:disabled { color: #8793a8; background: #edf0f6; }
            QPushButton#primary { background: #526edb; color: white; }
            QPushButton#primary:disabled { background: #d8dff1; color: #65718a; }
            QLabel#presetStatus { background: #e8eef8; border: 1px solid #cbd8ec;
                border-radius: 7px; padding: 9px; }
            QLabel#presetStatus[severity="error"] { color: #842029;
                background: #fff0f0; border-color: #df9b9b; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        intro = QLabel(
            "角色预设 = 参考人声 + 名称与参数，不需要为每个人物训练模型。\n"
            "音频保存后复制到软件专用目录，移动原文件也不会使新预设失效。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit(preset["name"] if preset else "")
        self.name.setObjectName("presetName")
        self.name.setPlaceholderText("给角色起个名字，例如：游戏旁白")
        self.name.setMaxLength(60)
        form.addRow("角色名称", self.name)
        self.language = QComboBox()
        self.language.addItems(["ZH", "EN", "JA", "ES"])
        self.language.setCurrentText((preset or {}).get("language", "ZH"))
        form.addRow("默认语言", self.language)
        layout.addLayout(form)
        file_row = QHBoxLayout()
        self.file = QLineEdit((preset or {}).get("reference_audio", ""))
        self.file.setReadOnly(True)
        self.file.setPlaceholderText("请选择一段有权使用的参考音频")
        file_row.addWidget(self.file, 1)
        self.browse = QPushButton("替换音频…" if preset else "选择音频…")
        self.browse.clicked.connect(self._browse)
        file_row.addWidget(self.browse)
        layout.addLayout(file_row)
        hint = QLabel(
            "支持 WAV / MP3 / FLAC / OGG，至少 1 秒，文件最大 25 MB。\n"
            "合成时模型最多使用开头 15 秒；建议开头是清晰、无配乐的单人人声。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.rights = QCheckBox("我确认有权使用该参考音频，并用于语音合成")
        self.rights.setVisible(not bool(preset))
        layout.addWidget(self.rights)
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("试听设备"))
        self.preview_device = QComboBox()
        fill_local_devices(self.preview_device, devices, monitor_device)
        style_combo_popup(self.preview_device)
        style_combo_popup(self.language)
        preview_row.addWidget(self.preview_device, 1)
        self.preview_button = QPushButton("播放参考音频")
        self.preview_button.clicked.connect(self._preview)
        preview_row.addWidget(self.preview_button)
        self.stop_button = QPushButton("停止试听")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_preview)
        preview_row.addWidget(self.stop_button)
        layout.addLayout(preview_row)
        preview_hint = QLabel(
            "播放整段参考音频，不是合成语音。请确认所选设备是耳机；"
            "不会主动输出到 VB-CABLE。"
        )
        preview_hint.setWordWrap(True)
        self.status = QLabel()
        self.status.setObjectName("presetStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self._set_status(
            "选择音频后，可先播放参考音频或直接保存。"
            if not preset
            else "可重命名或替换参考音频；不替换时保留原有音频及高级参数。"
        )
        layout.addWidget(self.status)
        layout.addWidget(preview_hint)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        self.delete_button = QPushButton("移除角色…")
        self.delete_button.setVisible(bool(preset))
        self.delete_button.clicked.connect(self._trash)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton("保存角色")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择角色参考音频",
            "",
            "参考音频 (*.wav *.mp3 *.flac *.ogg);;所有文件 (*.*)",
        )
        if path:
            self.source_path = Path(path)
            self.file.setText(path)
            self.rights.setChecked(False)
            self.rights.setVisible(True)
            self._set_status("已选择音频。可先播放，或确认使用权限后保存角色。")

    def _read_source(self):
        if self.source_path is None:
            return b"", ""
        with self.source_path.open("rb") as stream:
            data = stream.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("文件超过 25 MB，请缩小文件或截取所需片段。")
        return data, self.source_path.name

    def _request(self, method, path, *, timeout=(5, 45), **kwargs):
        response = requests.request(
            method, self.base_url + path, timeout=timeout, **kwargs
        )
        check_response(response)
        return response.json()

    def _run(self, callback, done, error=None):
        worker = _Task(callback)
        self._workers.add(worker)

        def success(result):
            self._workers.discard(worker)
            done(result)
            self._maybe_close()

        def failure(message):
            self._workers.discard(worker)
            (error or self._error)(message)
            self._maybe_close()

        worker.signals.done.connect(success)
        worker.signals.failed.connect(failure)
        QThreadPool.globalInstance().start(worker)

    def _set_busy(self, busy, preview=False):
        self._busy = busy
        for widget in (
            self.name,
            self.language,
            self.browse,
            self.rights,
            self.preview_device,
            self.save_button,
            self.delete_button,
            self.preview_button,
        ):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(not busy or preview)
        self.stop_button.setEnabled(busy and preview)

    def _set_status(self, message, *, error=False):
        severity = "error" if error else "info"
        if self.status.property("severity") != severity:
            self.status.setProperty("severity", severity)
            style = self.status.style()
            style.unpolish(self.status)
            style.polish(self.status)
        self.status.setText(message)

    def _error(self, message, *, action="操作未完成"):
        self._set_busy(False)
        self._set_status(f"{action}：{message}", error=True)

    def _save_error(self, message):
        self._error(message, action="保存失败")

    def _save(self):
        if not self.name.text().strip():
            self._save_error("请先填写角色名称。")
            return
        if self.source_path is None and not self.existing:
            self._save_error("请先选择参考音频。")
            return
        if self.source_path is not None and not self.rights.isChecked():
            self._save_error("请确认你有权使用所选参考音频。")
            return
        name, language = self.name.text().strip(), self.language.currentText()
        self._set_busy(True)
        self._set_status("正在检查音频并保存角色，原始文件不会被移动或删除…")

        def task():
            content, filename = self._read_source()
            if self.existing:
                path = "/v1/presets/" + quote(self.existing["name"], safe="")
                params = {"new_name": name, "filename": filename, "language": language}
                method = "PUT"
            else:
                path, method = "/v1/presets", "POST"
                params = {"name": name, "filename": filename, "language": language}
            return self._request(
                method,
                path,
                params=params,
                data=content,
                headers={"Content-Type": "application/octet-stream"},
            )

        def saved(result):
            self._set_busy(False)
            self.result_name = result["name"]
            self.accept()

        self._run(task, saved, self._save_error)

    def _preview(self):
        device = self.preview_device.currentData()
        if self.preview_device.currentIndex() < 0:
            self._preview_error("没有可用的明确耳机/扬声器设备，请连接后刷新设备。")
            return
        if self.source_path is None and not self.existing:
            self._preview_error("请先选择参考音频。")
            return
        self._previewing = True
        self._set_busy(True, preview=True)
        self._set_status("正在检查参考音频，通过后将在所选设备播放…")

        def task():
            content, filename = self._read_source()
            params = {"filename": filename}
            if device is not None:
                params["device_id"] = device
            if self.source_path is None and self.existing:
                params["preset"] = self.existing["name"]
            return self._request(
                "POST",
                "/v1/preset-preview",
                timeout=(5, PREVIEW_READ_TIMEOUT),
                params=params,
                data=content,
                headers={"Content-Type": "application/octet-stream"},
            )

        self._run(task, self._preview_done, self._preview_error)

    def _preview_done(self, result):
        self._previewing = False
        self._set_busy(bool(self._workers))
        duration = result["duration_seconds"]
        self._set_status(f"试听已结束。参考音频长 {duration:.2f} 秒，可保存角色。")

    def _maybe_close(self):
        if self._close_after_preview and not self._workers and not self._busy:
            super().reject()

    def _preview_error(self, message):
        self._previewing = False
        self._set_busy(bool(self._workers))
        self._set_status(f"试听失败：{message}", error=True)

    def _stop_preview(self):
        if self._stop_pending:
            return
        self._stop_pending = True
        self.stop_button.setEnabled(False)
        self._set_status("正在停止试听…")
        self._run(
            lambda: self._request("POST", "/v1/preset-preview/stop", json={}),
            lambda _: self._stop_acknowledged(),
            self._stop_failed,
        )

    def _stop_acknowledged(self):
        self._stop_pending = False
        if not self._previewing:
            self._set_busy(False)

    def _stop_failed(self, message):
        self._stop_pending = False
        self._set_status(f"停止请求失败：{message}", error=True)
        if not self._previewing:
            self._set_busy(False)
        else:
            self.stop_button.setEnabled(True)

    def _trash(self):
        if (
            QMessageBox.question(
                self,
                "移除角色？",
                "将角色移入软件预设回收区，不删除原始素材或模型。\n"
                "此操作不会立即释放磁盘空间。确定移除？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._set_busy(True)
        path = "/v1/presets/" + quote(self.existing["name"], safe="") + "/trash"

        def removed(result):
            self._set_busy(False)
            QMessageBox.information(
                self,
                "已移入角色回收区",
                "角色文件仍保留在：\n"
                + result["trash_path"]
                + "\n如需恢复，可将该文件夹移回 presets 并改回原角色名。",
            )
            self.result_name = ""
            self.accept()

        self._run(lambda: self._request("POST", path, json={"confirm": True}), removed)

    def reject(self):
        if self._previewing:
            self._close_after_preview = True
            self._stop_preview()
        elif not self._busy and not self._workers:
            super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
