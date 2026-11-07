"""Consistent device choices and readable Qt drop-downs in light or dark Windows."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractItemView, QComboBox

from .windows_audio import is_device_mapper


def style_combo_popup(combo: QComboBox) -> None:
    # Use a list box, not Qt's menu-style popup with hover scrollers.
    # Set this before creating the view so Qt sizes the entire popup to its rows.
    combo.setStyleSheet(combo.styleSheet() + "\nQComboBox { combobox-popup: 0; }")
    combo.setMaxVisibleItems(12)
    view = combo.view()
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    palette = view.palette()
    for role, color in (
        (QPalette.ColorRole.Base, "#ffffff"),
        (QPalette.ColorRole.Window, "#ffffff"),
        (QPalette.ColorRole.Text, "#1e2941"),
        (QPalette.ColorRole.WindowText, "#1e2941"),
        (QPalette.ColorRole.Highlight, "#304faa"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
    ):
        palette.setColor(role, QColor(color))
    view.setPalette(palette)
    view.setStyleSheet("""
        QAbstractItemView {
            background: #ffffff; color: #1e2941; font-size: 13px;
            selection-background-color: #304faa; selection-color: #ffffff;
            border: 1px solid #c1ccdf; outline: 0;
        }
        QAbstractItemView::item { min-height: 23px; padding: 3px 7px; }
        QAbstractItemView::item:selected { background: #304faa; color: #ffffff; }
    """)


def fill_local_devices(combo: QComboBox, devices, selected: int | None) -> None:
    combo.clear()
    combo.addItem("跟随系统默认", None)
    combo.setItemData(
        0,
        "使用当前 Windows 默认设备；下次播放或应用混音时重新读取。",
        Qt.ItemDataRole.ToolTipRole,
    )
    for device in devices:
        if not device.get("is_vb_cable") and not is_device_mapper(device["name"]):
            label = f"{device['name'].strip()} [{device['host_api']}]"
            combo.addItem(label, device["id"])
            combo.setItemData(combo.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
    combo.setCurrentIndex(
        max(0, combo.findData(selected)) if selected is not None else 0
    )
