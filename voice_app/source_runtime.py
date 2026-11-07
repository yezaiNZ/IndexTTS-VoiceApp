"""Check/repair only the repository's Qt packages, before importing its GUI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

QT_PROBE = """
import json
import PySide6, shiboken6
assert PySide6.__file__, 'PySide6 package initializer is missing'
from PySide6 import QtCore, QtGui, QtWidgets
print(json.dumps({'qt': QtCore.qVersion(), 'pyside': PySide6.__version__,
                  'shiboken': shiboken6.__version__}))
"""


def pinned_qt_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'["\']PySide6-Essentials==([0-9]+\.[0-9]+\.[0-9]+)["\']', text)
    if not match:
        raise ValueError("源码没有固定的 PySide6-Essentials 版本，不能安全地自动修复。")
    return match[1]


def probe_qt(python: Path, version: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(python), "-I", "-X", "utf8", "-c", QT_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if result.returncode:
            return False, (result.stderr or result.stdout).strip()[-1800:]
        versions = json.loads(result.stdout)
        if not all(
            versions.get(key) == version for key in ("qt", "pyside", "shiboken")
        ):
            return False, f"Qt 组件版本不一致：{versions}，要求 {version}。"
        return True, f"Qt / PySide6 / Shiboken {version} 真实加载通过。"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def ensure_qt(root: Path, python: Path, repair=False, progress=print) -> bool:
    version = pinned_qt_version(root)
    ready, detail = probe_qt(python, version)
    if ready:
        progress(detail)
        return True
    progress("源码界面环境检查失败：" + detail)
    if not repair:
        return False
    environment_dir = root / ".venv"
    scripts = environment_dir / ("Scripts" if os.name == "nt" else "bin")
    expected = scripts / ("python.exe" if os.name == "nt" else "python")
    if python.absolute() != expected.absolute():
        progress(
            "只允许修复本仓库 .venv，请使用 start_voice_app.bat；不会修改系统 Python。"
        )
        return False
    for directory in (environment_dir, scripts):
        if (
            directory.is_symlink()
            or getattr(directory.lstat(), "st_file_attributes", 0) & 0x400
        ):
            progress("仓库 Python 环境存在目录链接，已拒绝自动修复；不会修改其他环境。")
            return False
    uv = shutil.which("uv")
    if not uv:
        progress("未找到 uv，请先运行 setup_voice_app.bat；不会从未知网站下载 DLL。")
        return False
    progress("正在修复本仓库的两个 Qt 依赖；不会重装语音模型或推理环境…")
    environment = dict(os.environ)
    environment["UV_CACHE_DIR"] = str(root / ".uv-cache")
    command = [
        uv,
        "pip",
        "install",
        "--python",
        str(expected),
        "--no-deps",
        "--reinstall-package",
        "pyside6-essentials",
        "--reinstall-package",
        "shiboken6",
        f"PySide6-Essentials=={version}",
        f"shiboken6=={version}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        progress(f"Qt 修复未完成：{exc}；可再次启动重试。")
        return False
    if result.returncode:
        progress("Qt 修复未完成：" + (result.stderr or result.stdout).strip()[-1800:])
        return False
    ready, detail = probe_qt(python, version)
    progress(("Qt 修复后检查：" if ready else "Qt 修复后仍未通过检查：") + detail)
    return ready


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repair",
        action="store_true",
        help="repair only repository Qt dependencies if broken",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        return (
            0
            if ensure_qt(
                root,
                Path(sys.executable),
                args.repair,
                lambda message: print(message, flush=True),
            )
            else 1
        )
    except (OSError, ValueError) as exc:
        print(f"源码启动检查未完成：{exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
