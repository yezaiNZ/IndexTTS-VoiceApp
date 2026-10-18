"""Optional VB-Audio driver installation, with explicit consent and Windows UAC.

The original, unmodified Donationware package is bundled. Never install it
implicitly, change default devices, seed certificates, or reboot the machine.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import threading
import zipfile
from pathlib import Path

from .downloads import download_file, file_sha256
from .model_manifest import ModelFile


DRIVER_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
DRIVER_SHA256 = "b950e39f01af1d04ea623c8f6d8eb9b6ea5c477c637295fabf20631c85116bfb"
DRIVER_SIZE = 1318877


def cable_is_installed() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render",
        ) as key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, index)) as device:
                        state, _ = winreg.QueryValueEx(device, "DeviceState")
                        if state == 4:  # Stale, uninstalled endpoint.
                            continue
                        with winreg.OpenKey(device, "Properties") as properties:
                            for value in range(winreg.QueryInfoKey(properties)[1]):
                                _, data, _ = winreg.EnumValue(properties, value)
                                if (
                                    isinstance(data, str)
                                    and "vb-audio virtual cable" in data.casefold()
                                ):
                                    return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def bundle_driver(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "VBCABLE_Driver_Pack45.zip"
    spec = ModelFile(archive.name, "", "", archive.name, DRIVER_SIZE, DRIVER_SHA256)
    download_file(
        spec,
        archive,
        "auto",
        print,
        threading.Event(),
        folder / "download.log",
        urls=[("VB-Audio 官方", DRIVER_URL)],
    )
    return archive


def install_bundled_driver(bundle_dir: Path, data_dir: Path, progress, cancel) -> None:
    if cable_is_installed():
        progress("已检测到 VB-CABLE，保留现有驱动。")
        return
    archive = bundle_dir / "drivers" / "VBCABLE_Driver_Pack45.zip"
    if not archive.is_file() or file_sha256(archive) != DRIVER_SHA256:
        raise RuntimeError("安装包中的 VB-CABLE 驱动缺失或损坏，请重新获取完整安装包。")
    destination = data_dir / "drivers" / "VB-CABLE-Pack45"
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            relative = Path(entry.filename)
            target = destination / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not target.resolve().is_relative_to(destination.resolve())
            ):
                raise RuntimeError("驱动包路径校验失败。")
        source.extractall(destination)
    setup = destination / "VBCABLE_Setup_x64.exe"
    if not setup.is_file():
        raise RuntimeError("驱动包中缺少 64 位安装程序。")
    environment = dict(os.environ)
    environment["VOICE_APP_DRIVER_SETUP"] = str(setup)
    # Verify the vendor signature; do not alter trust stores to suppress prompts.
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$voiceSignature = Get-AuthenticodeSignature "
            "-LiteralPath $env:VOICE_APP_DRIVER_SETUP; "
            "if ($voiceSignature.Status -ne 'Valid' -or "
            "$voiceSignature.SignerCertificate.Subject "
            "-notmatch 'Vincent|VB-Audio') { exit 1 }",
        ],
        env=environment,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("VB-CABLE 安装程序的发布者签名验证失败，已阻止运行。")
    if cancel.is_set():
        from .downloads import DownloadCancelled

        raise DownloadCancelled("安装已取消。")
    progress(
        "正在打开 VB-Audio 官方安装器，请允许 Windows 管理员确认并点击 Install Driver。"
    )
    # Use the vendor's documented interactive installer, not undocumented flags.
    # Visible is intentional: the user must approve the driver and see its result.
    value = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", str(setup), None, str(destination), 1
    )
    if value <= 32:
        raise RuntimeError("驱动安装未获 Windows 许可。可以重试，或先选择仅本地试听。")
    progress("驱动安装器已打开；按官方提示安装并重启后，程序会自动检测 VB-CABLE。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    bundle_driver(args.bundle)
