"""Offline third-party notice viewer; no inference/audio environment is needed."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def verify_notices(root: Path) -> dict:
    problems = []
    checked = 0
    try:
        records = json.loads(
            (root / "third-party" / "files.sha256.json").read_text(encoding="utf-8")
        )
        if not isinstance(records, list) or not records:
            raise ValueError("Empty or invalid notice inventory")
        for record in records:
            relative = PurePosixPath(record["path"])
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] != "third-party"
                or "\\" in record["path"]
            ):
                problems.append("Unsafe inventory path")
                continue
            path = root.joinpath(*relative.parts)
            if not path.resolve().is_relative_to((root / "third-party").resolve()):
                problems.append("Notice path escapes its directory")
                continue
            if not path.is_file():
                problems.append(f"Missing: {relative}")
                continue
            with path.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != record["sha256"]:
                problems.append(f"Modified: {relative}")
            checked += 1
        inventory = json.loads(
            (root / "third-party" / "inventory.json").read_text(encoding="utf-8")
        )
        if not inventory.get("artifacts"):
            problems.append("Missing corresponding-source inventory")
        if not list(
            (root / "third-party" / "licenses" / "qtbase").rglob("LGPL-3.0-only.txt")
        ):
            problems.append("Missing LGPLv3 text")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(str(exc))
    return {"passed": not problems, "checked_files": checked, "problems": problems}


def create_notice_dialog(parent=None, root: Path | None = None):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QComboBox,
        QDialog,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QTextBrowser,
        QVBoxLayout,
    )

    from . import __version__

    root = root or resource_root()
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"第三方组件与许可 · IndexTTS Voice App v{__version__}")
    dialog.resize(900, 680)
    dialog.setMinimumSize(640, 480)
    dialog.setStyleSheet(
        "QDialog {background: #f3f5fa;} QLabel {color: #24324a;}"
        "QTextBrowser, QComboBox {background: white; color: #24324a; border: 1px solid #cbd5e1;}"
        "QPushButton {background: #edf1fb; color: #24324a; padding: 8px 14px;}"
    )
    layout = QVBoxLayout(dialog)
    notice = QLabel(
        "本软件使用 Qt / PySide6 / Shiboken6（The Qt Company 及其贡献者），"
        "适用的 LGPLv3 许可、对应源码和其他组件声明随完整发布包提供。"
        "第三方库的修改、替换和调试权利不受本项目额外限制。"
    )
    notice.setWordWrap(True)
    layout.addWidget(notice)
    choices = QComboBox()
    choices.setObjectName("licenseFiles")
    candidates = [root / "THIRD_PARTY.md", root / "third-party" / "README.md"]
    candidates += sorted((root / "third-party" / "licenses").rglob("*"))
    for path in candidates:
        if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
            choices.addItem(path.relative_to(root).as_posix(), str(path))
    layout.addWidget(choices)
    text = QTextBrowser()
    text.setObjectName("licenseText")
    layout.addWidget(text, 1)

    def selected(index):
        value = choices.itemData(index)
        if not value:
            text.setPlainText(
                "源码模式下可查看仓库 THIRD_PARTY.md；完整许可与源码随构建后的发行包提供。"
            )
            return
        try:
            text.setPlainText(Path(value).read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            text.setPlainText(f"无法打开许可文件：{exc}")

    choices.currentIndexChanged.connect(selected)
    selected(choices.currentIndex())
    buttons = QHBoxLayout()
    open_folder = QPushButton("打开许可与源码文件夹")
    folder = root / "third-party"
    open_folder.setEnabled(folder.is_dir())
    open_folder.clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    )
    buttons.addWidget(open_folder)
    buttons.addStretch(1)
    close = QPushButton("关闭")
    close.clicked.connect(dialog.accept)
    buttons.addWidget(close)
    layout.addLayout(buttons)
    return dialog


def show_third_party(parent=None):
    create_notice_dialog(parent).exec()


def verify_frontend(report_path: Path) -> int:
    """Explicit maintenance smoke test; never opens an audio device or backend."""
    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    from . import __version__

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    # The offscreen platform does not enumerate Windows system fonts itself.
    # Load an existing system font for this maintenance screenshot only;
    # no Windows font file is copied into the distribution.
    if sys.platform == "win32" and app.platformName() == "offscreen":
        font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Microsoft YaHei UI", 9))
    root = resource_root()
    result = verify_notices(root)
    dialog = create_notice_dialog(root=root)
    dialog.show()
    app.processEvents()
    result.update(
        {
            "version": __version__,
            "qt_version": qVersion(),
            "audio_opened": False,
            "model_loaded": False,
        }
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    result["image_saved"] = dialog.grab().save(str(report_path.with_suffix(".png")))
    dialog.close()
    app.processEvents()
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if result["passed"] and result["image_saved"] else 1
