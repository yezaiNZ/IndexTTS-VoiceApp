from types import SimpleNamespace

import pytest

from voice_app import audio
from voice_app.schemas import AudioDevice, DesktopSettings, MixerStartRequest


def device(index, name, *, default=False, cable=False, host="MME"):
    return AudioDevice(
        id=index,
        name=name,
        host_api=host,
        channels=2,
        default_samplerate=44100,
        is_default=default,
        is_vb_cable=cable,
    )


def test_windows_default_resolves_real_endpoint_on_each_query(monkeypatch):
    monkeypatch.setattr(audio, "sys", SimpleNamespace(platform="win32"))
    current = {True: "USB Mic", False: "Headphones"}
    monkeypatch.setattr(
        audio, "preferred_wave_name", lambda *, input_device: current[input_device]
    )
    rows = [
        ("Microsoft 声音映射器 - Output", 0, 0, 2),
        ("Headphones", 0, 0, 2),
        ("Headphones", 1, 0, 2),
        ("Speakers", 0, 0, 2),
        ("USB Mic", 0, 1, 0),
    ]
    monkeypatch.setattr(
        audio.sd,
        "query_hostapis",
        lambda: [{"name": "MME"}, {"name": "Windows DirectSound"}],
    )
    monkeypatch.setattr(
        audio.sd,
        "query_devices",
        lambda: [
            {
                "name": name,
                "hostapi": host,
                "max_input_channels": inputs,
                "max_output_channels": outputs,
            }
            for name, host, inputs, outputs in rows
        ],
    )
    assert (
        audio.resolve_system_device(audio.list_output_devices(), input_device=False)
        == 1
    )
    assert (
        audio.resolve_system_device(audio.list_input_devices(), input_device=True) == 4
    )
    current[False] = "Speakers"
    assert (
        audio.resolve_system_device(audio.list_output_devices(), input_device=False)
        == 3
    )
    rows.append(("Speakers", 0, 0, 2))
    assert not any(item.is_default for item in audio.list_output_devices())


@pytest.mark.parametrize("input_device", [False, True])
def test_system_defaults_fail_closed_for_missing_mapper_or_cable(input_device):
    for choices, reason in (
        ([device(9, "Headphones")], "找不到"),
        ([device(9, "Microsoft 声音映射器", default=True)], "找不到"),
        ([device(14, "VB-Audio", default=True, cable=True)], "VB-CABLE"),
    ):
        with pytest.raises(ValueError, match=reason):
            audio.resolve_system_device(choices, input_device=input_device)


def test_monitor_follows_system_but_manual_and_virtual_outputs_stay_fixed(monkeypatch):
    opened = []
    current = [9]
    monkeypatch.setattr(
        audio,
        "list_output_devices",
        lambda: [
            device(9, "Headphones", default=current[0] == 9),
            device(10, "Speakers", default=current[0] == 10),
            device(14, "CABLE Input", cable=True),
        ],
    )
    monkeypatch.setattr(audio, "_Sink", lambda index: opened.append(index))
    audio.AudioRouter(None, True, None)
    assert opened == [9]
    opened.clear()
    current[0] = 10
    audio.AudioRouter(14, True, None)
    assert opened == [14, 10]
    opened.clear()
    audio.AudioRouter(None, True, 9)
    assert opened == [9]


def test_mixer_follows_current_default_mic_on_next_start(monkeypatch):
    opened = []
    current = [2]

    class Stream:
        def __init__(self, *, device, **kwargs):
            self.active = False
            opened.append(device)

        def start(self):
            self.active = True

        def stop(self):
            self.active = False

        def close(self):
            pass

    monkeypatch.setattr(
        audio,
        "list_input_devices",
        lambda: [
            device(2, "USB Mic", default=current[0] == 2),
            device(3, "Headset Mic", default=current[0] == 3),
        ],
    )
    monkeypatch.setattr(
        audio,
        "list_output_devices",
        lambda: [
            device(14, "CABLE Input", cable=True),
        ],
    )
    monkeypatch.setattr(audio.sd, "check_input_settings", lambda **kwargs: None)
    monkeypatch.setattr(audio.sd, "check_output_settings", lambda **kwargs: None)
    monkeypatch.setattr(audio.sd, "Stream", Stream)
    mixer = audio.MicrophoneMixer()
    request = MixerStartRequest(input_device=None, output_device=14)
    try:
        assert mixer.start(**request.model_dump())["input_device"] == 2
        current[0] = 3
        assert mixer.start(**request.model_dump())["input_device"] == 3
        assert mixer.start(input_device=2, output_device=14)["input_device"] == 2
        assert opened == [(2, 14), (3, 14), (2, 14)]
    finally:
        mixer.stop()


def test_saved_default_mic_restores_only_when_enabled():
    from voice_app.api import _ensure_saved_mixer

    calls = []
    runtime = SimpleNamespace(
        mixer=SimpleNamespace(start=lambda **kwargs: calls.append(kwargs))
    )
    request = SimpleNamespace(output_device=14)
    saved = DesktopSettings(output_device=14)
    settings = SimpleNamespace(load=lambda: saved)
    _ensure_saved_mixer(runtime, settings, request)
    assert calls == []
    saved.microphone_enabled = True
    _ensure_saved_mixer(runtime, settings, request)
    assert calls[0]["input_device"] is None and calls[0]["output_device"] == 14


def test_desktop_default_choices_and_manual_settings_are_preserved(monkeypatch):
    from voice_app.desktop_qt import VoiceDesktopQt

    monkeypatch.setattr(VoiceDesktopQt, "_refresh_data", lambda self: None)
    window = VoiceDesktopQt("http://127.0.0.1:1", model_ready=True)
    data = {
        "presets": [],
        "inputs": [
            device(0, "Microsoft 声音映射器 - Input").model_dump(),
            device(2, "USB Mic").model_dump(),
        ],
        "outputs": [
            device(9, "Headphones").model_dump(),
            device(14, "CABLE Input", cable=True).model_dump(),
        ],
        "settings": {},
    }
    try:
        window._apply_data(data)
        assert window.microphone_device.currentText() == "跟随系统默认"
        assert window.monitor_device.currentText() == "跟随系统默认"
        assert window.microphone_device.count() == window.monitor_device.count() == 2
        assert window.output_device.currentData() is None
        assert (
            window.monitor_enabled.isChecked()
            and not window.microphone_enabled.isChecked()
        )
        assert (
            window.output_device.findData(9) < 0
        )  # Headphones are not a virtual route.
        data["settings"] = {
            "output_device": 14,
            "monitor_device": 9,
            "microphone_device": 2,
        }
        window._apply_data(data)
        assert window.output_device.currentData() == 14
        assert window.monitor_device.currentData() == 9
        assert window.microphone_device.currentData() == 2

        saved = []
        monkeypatch.setattr(window, "_put", lambda path, body: saved.append(body))
        monkeypatch.setattr(window, "_configure_mixer", lambda body: None)
        monkeypatch.setattr(
            window, "_run_async", lambda task, done, error: done(task())
        )
        window.monitor_device.setCurrentIndex(0)
        window.microphone_device.setCurrentIndex(0)
        window.apply_mixer()
        assert (
            saved[0]["monitor_device"] is None and saved[0]["microphone_device"] is None
        )
        assert saved[0]["output_device"] == 14
    finally:
        window.close()


def test_popup_colors_are_readable_with_dark_application_palette():
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication, QComboBox

    from voice_app.qt_audio_choices import style_combo_popup

    app = QApplication.instance() or QApplication([])
    original = app.palette()
    dark = QPalette(original)
    dark.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
    dark.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
    app.setPalette(dark)
    combo = QComboBox()
    try:
        combo.addItems(["跟随系统默认", "耳机 (Arctis Nova 3) [MME]"])
        style_combo_popup(combo)
        combo.show()
        combo.showPopup()
        app.processEvents()
        palette = combo.view().palette()
        assert palette.color(QPalette.ColorRole.Base).name() == "#ffffff"
        assert palette.color(QPalette.ColorRole.Text).name() == "#1e2941"
        assert palette.color(QPalette.ColorRole.Highlight).name() == "#304faa"
        assert palette.color(QPalette.ColorRole.HighlightedText).name() == "#ffffff"
    finally:
        combo.hidePopup()
        combo.close()
        app.setPalette(original)


def test_long_device_popup_uses_bounded_list_and_wheel_scroll(monkeypatch):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QStyle

    from voice_app.desktop_qt import VoiceDesktopQt

    monkeypatch.setattr(VoiceDesktopQt, "_refresh_data", lambda self: None)
    window = VoiceDesktopQt("http://127.0.0.1:1", model_ready=True)
    app = QApplication.instance()
    combo = window.monitor_device
    try:
        combo.addItems([f"音频设备 {index} [MME]" for index in range(55)])
        window.show()
        combo.showPopup()
        app.processEvents()

        view = combo.view()
        popup = view.window()
        bar = view.verticalScrollBar()
        assert (
            combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup, None, combo)
            == 0
        )
        assert view.isVisible() and bar.isVisible() and bar.maximum() > bar.minimum()
        assert view.height() <= combo.maxVisibleItems() * view.sizeHintForRow(0) + 8
        assert popup.height() - view.height() <= 8  # No blank hover-scroller areas.

        previous_position = bar.value()
        previous_choice = combo.currentIndex()
        center = view.viewport().rect().center()
        event = QWheelEvent(
            QPointF(center),
            QPointF(view.viewport().mapToGlobal(center)),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        app.sendEvent(view.viewport(), event)
        app.processEvents()
        assert bar.value() > previous_position
        assert combo.currentIndex() == previous_choice
    finally:
        combo.hidePopup()
        window.close()
