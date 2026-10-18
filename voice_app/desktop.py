from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable

import requests


class VoiceDesktop:
    def __init__(
        self,
        base_url: str,
        on_close: Callable[[], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.on_close_callback = on_close
        self.root = tk.Tk()
        self.root.title("IndexTTS Voice App")
        self.root.geometry("920x900")
        self.root.minsize(820, 780)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.devices: dict[str, int] = {}
        self.device_records: list[dict] = []
        self.input_devices: dict[str, int] = {}
        self.input_device_records: list[dict] = []
        self.generating = False
        self._setup_style()
        self._build()
        self._run_background(self._refresh_data)

    def _setup_style(self):
        self.root.tk.call("tk", "scaling", 1.1)
        self.root.configure(background="#f3f5fa")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#f3f5fa")
        style.configure("Header.TFrame", background="#1f2a44")
        style.configure(
            "Title.TLabel",
            background="#1f2a44",
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 19, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#1f2a44",
            foreground="#cbd5e8",
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Card.TLabelframe",
            background="#ffffff",
            bordercolor="#d9dfeb",
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background="#ffffff",
            foreground="#26334f",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure("Card.TLabel", background="#ffffff", foreground="#27334b")
        style.configure("Hint.TLabel", background="#ffffff", foreground="#66728a")
        style.configure(
            "Section.TLabel",
            background="#f3f5fa",
            foreground="#26334f",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "Status.TLabel",
            background="#e8edf7",
            foreground="#31415f",
            padding=(10, 5),
        )
        style.configure(
            "Accent.TButton",
            background="#4169e1",
            foreground="#ffffff",
            padding=(14, 7),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#3557c7")])
        style.configure("Danger.TButton", padding=(12, 7))
        style.configure("TCombobox", padding=4)
        style.configure("TEntry", padding=4)
        style.configure("TCheckbutton", background="#ffffff")

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer = ttk.Frame(self.root, padding=(22, 18, 22, 18), style="App.TFrame")
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer, padding=(16, 13), style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="IndexTTS Voice App", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="本地角色语音 · 实时混音 · 虚拟麦克风",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        ttk.Label(outer, text="要让角色说什么？", style="Section.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 6)
        )
        self.text = ScrolledText(
            outer,
            height=10,
            wrap=tk.WORD,
            font=("Microsoft YaHei UI", 11),
            relief="solid",
            borderwidth=1,
            background="#ffffff",
            foreground="#27334b",
            insertbackground="#4169e1",
        )
        self.text.grid(row=2, column=0, sticky="nsew")

        form = ttk.LabelFrame(
            outer, text="角色与音频路由", padding=(16, 10), style="Card.TLabelframe"
        )
        form.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="角色预设", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=5
        )
        self.preset = ttk.Combobox(form, state="readonly")
        self.preset.grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(form, text="语言", style="Card.TLabel").grid(
            row=0, column=2, sticky="w", padx=(16, 8), pady=5
        )
        self.language = ttk.Combobox(
            form, values=("ZH", "EN", "JA", "ES"), state="readonly", width=8
        )
        self.language.set("ZH")
        self.language.grid(row=0, column=3, sticky="w", pady=5)

        ttk.Label(form, text="参考音频", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=5
        )
        self.reference = ttk.Entry(form)
        self.reference.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Button(form, text="浏览…", command=self._browse_audio).grid(
            row=1, column=3, sticky="e", pady=5
        )

        ttk.Label(form, text="虚拟输出", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=5
        )
        self.output_device = ttk.Combobox(form, state="readonly")
        self.output_device.grid(row=2, column=1, columnspan=3, sticky="ew", pady=5)
        ttk.Label(
            form,
            text="目标软件的麦克风选择 CABLE Output；本程序向 CABLE Input 输出。",
            style="Hint.TLabel",
        ).grid(row=3, column=1, columnspan=3, sticky="w", pady=(0, 5))

        self.microphone_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form,
            text="真人麦克风透传",
            variable=self.microphone_enabled,
            command=self.apply_mixer,
        ).grid(row=4, column=0, sticky="w", pady=5)
        self.microphone_device = ttk.Combobox(form, state="readonly")
        self.microphone_device.grid(row=4, column=1, columnspan=3, sticky="ew", pady=5)

        self.monitor_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form,
            text="同时本地监听",
            variable=self.monitor_enabled,
            style="TCheckbutton",
        ).grid(row=5, column=0, sticky="w", pady=5)
        self.monitor_device = ttk.Combobox(form, state="readonly")
        self.monitor_device.grid(row=5, column=1, columnspan=3, sticky="ew", pady=5)

        self.microphone_gain = tk.DoubleVar(value=1.0)
        self.tts_gain = tk.DoubleVar(value=1.0)
        ttk.Label(form, text="真人音量", style="Card.TLabel").grid(
            row=6, column=0, sticky="w", pady=5
        )
        ttk.Scale(form, from_=0.0, to=2.0, variable=self.microphone_gain).grid(
            row=6, column=1, sticky="ew", pady=5
        )
        ttk.Label(form, text="角色音量", style="Card.TLabel").grid(
            row=6, column=2, sticky="w", padx=(16, 8), pady=5
        )
        ttk.Scale(form, from_=0.0, to=2.0, variable=self.tts_gain).grid(
            row=6, column=3, sticky="ew", pady=5
        )
        ttk.Label(
            form,
            text="开启后，真人声音与角色语音会合并到同一个 CABLE Output。",
            style="Hint.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Button(
            form, text="应用混音设置", command=self.apply_mixer, style="Accent.TButton"
        ).grid(row=7, column=3, sticky="e", pady=(2, 0))

        advanced = ttk.LabelFrame(
            outer,
            text="生成参数（通常保持默认即可）",
            padding=(16, 12),
            style="Card.TLabelframe",
        )
        advanced.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        for column in range(10):
            advanced.columnconfigure(column, weight=1 if column % 2 else 0)
        self.top_p = self._field(advanced, 0, "Top P", "0.8")
        self.top_k = self._field(advanced, 2, "Top K", "30")
        self.temperature = self._field(advanced, 4, "温度", "0.8")
        self.segment_tokens = self._field(advanced, 6, "分段长度", "120")
        self.duration_factor = self._field(advanced, 8, "语速系数", "1.0")

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky="ew", pady=(18, 0))
        actions.columnconfigure(0, weight=1)
        self.generate_button = ttk.Button(
            actions, text="▶ 生成并输出", command=self.generate, style="Accent.TButton"
        )
        self.generate_button.grid(row=0, column=1, padx=(8, 0))
        self.stop_button = ttk.Button(
            actions,
            text="停止生成",
            command=self.stop,
            state="disabled",
            style="Danger.TButton",
        )
        self.stop_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(
            actions,
            text="刷新设备列表",
            command=lambda: self._run_background(self._refresh_data),
        ).grid(row=0, column=3, padx=(8, 0))
        self.status = ttk.Label(
            actions, text="正在连接本地服务…", style="Status.TLabel"
        )
        self.status.grid(row=0, column=0, sticky="w")

    def _field(self, parent, column: int, label: str, default: str) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(
            row=0, column=column, sticky="w", padx=(0, 5)
        )
        entry = ttk.Entry(parent, width=8)
        entry.insert(0, default)
        entry.grid(row=0, column=column + 1, sticky="ew", padx=(0, 14))
        return entry

    def _browse_audio(self):
        path = filedialog.askopenfilename(
            title="选择参考音频",
            filetypes=(
                ("音频文件", "*.wav *.mp3 *.ogg *.flac *.m4a"),
                ("所有文件", "*.*"),
            ),
        )
        if path:
            self.reference.delete(0, tk.END)
            self.reference.insert(0, path)

    def _refresh_data(self):
        try:
            presets = self._get("/v1/presets")
            devices = self._get("/v1/devices")
            input_devices = self._get("/v1/input-devices")
            settings = self._get("/v1/settings")
        except Exception as exc:
            self._ui(lambda exc=exc: self._set_status(f"连接失败：{exc}"))
            return
        self._ui(lambda: self._apply_data(presets, devices, input_devices, settings))
        self._run_background(self._preload_model)

    def _apply_data(
        self,
        presets: list[dict],
        devices: list[dict],
        input_devices: list[dict],
        settings: dict,
    ):
        preset_names = [item["name"] for item in presets]
        self.preset["values"] = ("", *preset_names)
        last_preset = settings.get("last_preset", "")
        self.preset.set(
            last_preset
            if last_preset in preset_names
            else (preset_names[0] if preset_names else "")
        )

        self.device_records = devices
        labels = []
        self.devices.clear()
        for device in devices:
            marker = " [VB-CABLE]" if device.get("is_vb_cable") else ""
            if device.get("is_vb_cable") and device.get("host_api") == "MME":
                marker = " [VB-CABLE 推荐]"
            label = f"{device['id']}: {device['name']} ({device['host_api']}){marker}"
            labels.append(label)
            self.devices[label] = int(device["id"])
        self.output_device["values"] = labels
        self.monitor_device["values"] = labels
        self._select_device(
            self.output_device, settings.get("output_device"), prefer_cable=True
        )
        self._select_device(
            self.monitor_device, settings.get("monitor_device"), prefer_default=True
        )
        self.input_device_records = input_devices
        input_labels = []
        self.input_devices.clear()
        for device in input_devices:
            if device.get("is_vb_cable"):
                continue
            label = f"{device['id']}: {device['name']} ({device['host_api']})"
            input_labels.append(label)
            self.input_devices[label] = int(device["id"])
        self.microphone_device["values"] = input_labels
        self._select_input_device(settings.get("microphone_device"))
        self.microphone_enabled.set(bool(settings.get("microphone_enabled", False)))
        self.microphone_gain.set(float(settings.get("microphone_gain", 1.0)))
        self.tts_gain.set(float(settings.get("tts_gain", 1.0)))
        self.monitor_enabled.set(bool(settings.get("monitor_enabled", False)))
        self.language.set(settings.get("language", "ZH"))
        if settings.get("last_reference_audio"):
            self.reference.delete(0, tk.END)
            self.reference.insert(0, settings["last_reference_audio"])
        self._replace(self.top_p, settings.get("top_p", 0.8))
        self._replace(self.top_k, settings.get("top_k", 30))
        self._replace(self.temperature, settings.get("temperature", 0.8))
        self._replace(
            self.segment_tokens, settings.get("max_text_tokens_per_segment", 120)
        )
        self._replace(self.duration_factor, settings.get("duration_factor", 1.0))
        self._set_status("服务已连接，正在加载模型…")
        if self.microphone_enabled.get():
            try:
                mixer = self._mixer_payload()
            except ValueError as exc:
                self._set_status(f"麦克风透传未启动：{exc}")
            else:
                self._run_background(lambda: self._configure_mixer(mixer, True))

    def _select_device(
        self, combo, configured, prefer_cable=False, prefer_default=False
    ):
        candidates = self.device_records
        selected = next((d for d in candidates if d["id"] == configured), None)
        if prefer_cable:
            configured_is_safe_cable = bool(
                selected
                and selected.get("is_vb_cable")
                and selected.get("host_api") == "MME"
                and int(selected.get("default_samplerate", 0)) == 44100
            )
            if not configured_is_safe_cable:
                selected = next(
                    (
                        d
                        for d in candidates
                        if d.get("is_vb_cable")
                        and d.get("host_api") == "MME"
                        and int(d.get("default_samplerate", 0)) == 44100
                    ),
                    None,
                )
            selected = selected or next(
                (
                    d
                    for d in candidates
                    if d.get("is_vb_cable") and d.get("host_api") == "MME"
                ),
                None,
            )
            selected = selected or next(
                (d for d in candidates if d.get("is_vb_cable")), None
            )
        if selected is None and prefer_default:
            selected = next((d for d in candidates if d.get("is_default")), None)
        if selected is None and candidates:
            selected = candidates[0]
        if selected is not None:
            label = next(
                (
                    label
                    for label, value in self.devices.items()
                    if value == selected["id"]
                ),
                "",
            )
            combo.set(label)

    def _select_input_device(self, configured):
        candidates = [
            device
            for device in self.input_device_records
            if not device.get("is_vb_cable")
        ]
        selected = next((d for d in candidates if d["id"] == configured), None)
        if selected is None:
            preferred = [
                device
                for device in candidates
                if device.get("host_api") == "MME"
                and not any(
                    word in device.get("name", "").casefold()
                    for word in (
                        "virtual",
                        "ivcam",
                        "todesk",
                        "netease",
                        "网易",
                    )
                )
            ]
            selected = next(
                (
                    device
                    for device in preferred
                    if "麦克风" in device.get("name", "")
                    or "microphone" in device.get("name", "").casefold()
                ),
                None,
            )
            selected = selected or (preferred[0] if preferred else None)
        if selected is None and candidates:
            selected = candidates[0]
        if selected is not None:
            label = next(
                (
                    label
                    for label, value in self.input_devices.items()
                    if value == selected["id"]
                ),
                "",
            )
            self.microphone_device.set(label)

    def _preload_model(self):
        try:
            response = requests.post(self.base_url + "/v1/model/load", timeout=None)
            response.raise_for_status()
            self._ui(lambda: self._set_status("模型已加载，可以生成"))
        except Exception as exc:
            self._ui(lambda exc=exc: self._set_status(f"模型加载失败：{exc}"))

    def generate(self):
        if self.generating:
            return
        try:
            payload = self._payload()
            mixer = self._mixer_payload()
        except ValueError as exc:
            messagebox.showwarning("参数不完整", str(exc), parent=self.root)
            return
        self.generating = True
        self.generate_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._set_status("正在生成；音频块就绪后会立即输出…")
        self._run_background(lambda: self._generate_request(payload, mixer))

    def _payload(self) -> dict:
        text = self.text.get("1.0", tk.END).strip()
        if not text:
            raise ValueError("请先输入文本。")
        preset = self.preset.get().strip() or None
        reference = self.reference.get().strip() or None
        if not preset and not reference:
            raise ValueError("请选择角色预设或参考音频。")
        output_device = self.devices.get(self.output_device.get())
        if output_device is None:
            raise ValueError("请选择 VB-CABLE 输出设备。")
        monitor_device = self.devices.get(self.monitor_device.get())
        return {
            "text": text,
            "preset": preset,
            "reference_audio": reference,
            "language": self.language.get() or "ZH",
            "output_device": output_device,
            "monitor_enabled": self.monitor_enabled.get(),
            "monitor_device": monitor_device,
            "save_output": False,
            "generation": {
                "top_p": float(self.top_p.get()),
                "top_k": int(self.top_k.get()),
                "temperature": float(self.temperature.get()),
                "max_text_tokens_per_segment": int(self.segment_tokens.get()),
                "duration_factor": float(self.duration_factor.get()),
            },
        }

    def _mixer_payload(self) -> dict:
        output_device = self.devices.get(self.output_device.get())
        if output_device is None:
            raise ValueError("请选择 VB-CABLE 输出设备。")
        input_device = self.input_devices.get(self.microphone_device.get())
        if self.microphone_enabled.get() and input_device is None:
            raise ValueError("请选择用于真人说话的物理麦克风。")
        return {
            "enabled": self.microphone_enabled.get(),
            "input_device": input_device,
            "output_device": output_device,
            "mic_gain": float(self.microphone_gain.get()),
            "tts_gain": float(self.tts_gain.get()),
        }

    def _generate_request(self, payload: dict, mixer: dict):
        try:
            self._save_settings(payload, mixer)
            self._configure_mixer(mixer, False)
            with requests.post(
                self.base_url + "/v1/tts/stream",
                json=payload,
                stream=True,
                timeout=(15, None),
            ) as response:
                response.raise_for_status()
                output_path = response.headers.get("X-Output-File", "")
                for _ in response.iter_content(chunk_size=65536):
                    pass
            message = "生成完成"
            if output_path:
                message += f"，已保存到 {output_path}"
            self._ui(lambda: self._generation_finished(message))
        except Exception as exc:
            self._ui(lambda exc=exc: self._generation_finished(f"生成失败：{exc}"))

    def _save_settings(self, payload: dict, mixer: dict):
        generation = payload["generation"]
        value = {
            "output_device": payload["output_device"],
            "microphone_enabled": mixer["enabled"],
            "microphone_device": mixer["input_device"],
            "microphone_gain": mixer["mic_gain"],
            "tts_gain": mixer["tts_gain"],
            "monitor_enabled": payload["monitor_enabled"],
            "monitor_device": payload["monitor_device"],
            "last_preset": payload["preset"] or "",
            "last_reference_audio": payload["reference_audio"] or "",
            "language": payload["language"],
            "top_p": generation["top_p"],
            "top_k": generation["top_k"],
            "temperature": generation["temperature"],
            "max_text_tokens_per_segment": generation["max_text_tokens_per_segment"],
            "duration_factor": generation["duration_factor"],
        }
        response = requests.put(self.base_url + "/v1/settings", json=value, timeout=5)
        response.raise_for_status()

    def apply_mixer(self):
        try:
            mixer = self._mixer_payload()
        except ValueError as exc:
            messagebox.showwarning("麦克风设置", str(exc), parent=self.root)
            return
        self._set_status("正在应用麦克风混音设置…")
        self._run_background(lambda: self._apply_mixer_settings(mixer))

    def _apply_mixer_settings(self, mixer: dict):
        try:
            response = requests.get(self.base_url + "/v1/settings", timeout=5)
            response.raise_for_status()
            settings = response.json()
            settings.update(
                {
                    "output_device": mixer["output_device"],
                    "microphone_enabled": mixer["enabled"],
                    "microphone_device": mixer["input_device"],
                    "microphone_gain": mixer["mic_gain"],
                    "tts_gain": mixer["tts_gain"],
                }
            )
            response = requests.put(
                self.base_url + "/v1/settings", json=settings, timeout=5
            )
            response.raise_for_status()
            self._configure_mixer(mixer, True)
        except Exception as exc:
            self._ui(lambda exc=exc: self._set_status(f"麦克风设置失败：{exc}"))

    def _configure_mixer(self, mixer: dict, show_success: bool):
        try:
            if mixer["enabled"]:
                body = {key: value for key, value in mixer.items() if key != "enabled"}
                response = requests.post(
                    self.base_url + "/v1/mixer/start", json=body, timeout=10
                )
                success = "麦克风透传已开启：真人与角色语音将一起输出"
            else:
                response = requests.post(self.base_url + "/v1/mixer/stop", timeout=10)
                success = "麦克风透传已关闭"
            response.raise_for_status()
            if show_success:
                self._ui(lambda: self._set_status(success))
        except Exception as exc:
            if show_success:
                self._ui(lambda exc=exc: self._set_status(f"麦克风设置失败：{exc}"))
            else:
                raise

    def stop(self):
        self._set_status("正在请求停止；会在当前分段完成后停止…")
        self._run_background(self._stop_request)

    def _stop_request(self):
        try:
            response = requests.post(self.base_url + "/v1/stop", timeout=5)
            response.raise_for_status()
        except Exception as exc:
            self._ui(lambda exc=exc: self._set_status(f"停止失败：{exc}"))

    def _generation_finished(self, message: str):
        self.generating = False
        self.generate_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._set_status(message)

    def _get(self, path: str):
        response = requests.get(self.base_url + path, timeout=10)
        response.raise_for_status()
        return response.json()

    def _run_background(self, target):
        threading.Thread(target=target, daemon=True).start()

    def _ui(self, callback):
        self.root.after(0, callback)

    def _set_status(self, text: str):
        self.status.configure(text=text)

    @staticmethod
    def _replace(entry: ttk.Entry, value):
        entry.delete(0, tk.END)
        entry.insert(0, str(value))

    def close(self):
        if self.generating:
            self._run_background(self._stop_request)
        if self.on_close_callback is not None:
            self.on_close_callback()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
