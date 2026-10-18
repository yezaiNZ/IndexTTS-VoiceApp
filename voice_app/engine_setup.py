"""Install a private, pinned IndexTTS engine without bundling its model."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from .downloads import DownloadCancelled, download_file
from .model_manifest import (
    ALL_MODELS,
    AUXILIARY_MODELS,
    MANIFEST_VERSION,
    PRIMARY_MODELS,
)
from .processes import external_program, independent_environment

SOURCE_COMMIT = "ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c"
SOURCE_SHA256 = "a0e2c53621b090e142faee23e6815b9074548cffca2ff13d036a7025f6285468"
SOURCE_URL = f"https://codeload.github.com/index-tts/index-tts/zip/{SOURCE_COMMIT}"
MODEL_REPO = "IndexTeam/IndexTTS-2.5"
MODEL_REVISION = "c39ce5ba981572cb187443877ff559dfb246ce63"
MODEL_FILES = (
    "LICENSE",
    "config.yaml",
    "codec.pth",
    "gpt.pth",
    "s2mel.pth",
    "feat1.pt",
    "feat2.pt",
    "multilingual_zh_ja_yue_char_del.tiktoken",
    "wav2vec2bert_stats.pt",
)
MODEL_SHA256 = {
    "LICENSE": "cc7da9ea0f8a97ef15ab3bf0389e636ce79ffca1aef5489520796ac87d87a87b",
    "config.yaml": "18adf417be3e8f5e2e48e30f7420c719170a6870619436250f360d626877870e",
    "codec.pth": "d15cbed16a40f478438c961fb043f68dfa6353bf56c966761315db3433e9722c",
    "gpt.pth": "43a8f4c30eccdf201958d3b9713511482c19d56dc20b0b1c4ee1e6b080b19d85",
    "s2mel.pth": "9b1b0003fc189c94cc349758d7ebc25f903b7eb2de4602879959cc64ce816456",
    "feat1.pt": "f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4",
    "feat2.pt": "9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80",
    "multilingual_zh_ja_yue_char_del.tiktoken": (
        "747979631e813193436aabcff7c1c235d37de8097b71c563ec8b63b7a515c718"
    ),
    "wav2vec2bert_stats.pt": (
        "c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2"
    ),
}
MARKER = ".voice-app-managed.json"
PYTHON_VERSION = "3.11.16"
APP_ENVIRONMENT = ".voice-app-venv"
Progress = Callable[[str], None]


class InstallCancelled(DownloadCancelled):
    pass


class InstallCommandError(RuntimeError):
    def __init__(self, code: int, detail: str, log_path: Path):
        self.code, self.detail = code, detail
        super().__init__(f"退出码 {code}：{detail}\n详细记录：{log_path}")


def engine_python(root: Path) -> Path:
    managed = root / APP_ENVIRONMENT / "Scripts" / "python.exe"
    return managed if managed.is_file() else root / ".venv" / "Scripts" / "python.exe"


def engine_environment(root: Path, data_dir: Path | None = None) -> dict[str, str]:
    environment = independent_environment(os.environ)
    for name, folder in (
        ("UV_CACHE_DIR", ".uv-cache"),
        ("UV_PYTHON_INSTALL_DIR", ".uv-python"),
        ("HF_HOME", ".hf-cache"),
        ("MODELSCOPE_CACHE", ".modelscope-cache"),
        ("NLTK_DATA", ".nltk-data"),
    ):
        environment[name] = str(root / folder)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONUTF8"] = "1"
    # Explicit physical Python paths avoid the minor-version junction which
    # some Windows installations refuse to traverse (ERROR_UNTRUSTED_MOUNT_POINT).
    environment["UV_PROJECT_ENVIRONMENT"] = str(root / APP_ENVIRONMENT)
    environment["UV_PYTHON_INSTALL_BIN"] = "0"
    environment["UV_PYTHON_INSTALL_REGISTRY"] = "0"
    for name in (
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "UV_MANAGED_PYTHON",
        "UV_NO_MANAGED_PYTHON",
        "UV_PYTHON_PREFERENCE",
    ):
        environment.pop(name, None)
    if data_dir is not None:
        environment["VOICE_APP_DATA_DIR"] = str(data_dir)
    return environment


def model_inventory_errors(root: Path, verify_hash: bool = False) -> list[str]:
    """Directory existence is never evidence that a model is complete."""
    errors = []
    checkpoints = root / "checkpoints"
    for item in ALL_MODELS:
        path = checkpoints / item.path
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != item.size
            or (verify_hash and _sha256(path) != item.sha256)
        ):
            errors.append(item.path)
    return errors


def preparation_is_current(root: Path) -> bool:
    try:
        receipt = json.loads(
            (root / ".voice-app-prepared.json").read_text(encoding="utf-8")
        )
        return (
            isinstance(receipt, dict)
            and receipt.get("manifest") == MANIFEST_VERSION
            and not model_inventory_errors(root)
        )
    except (OSError, ValueError):
        return False


def is_managed_engine(root: Path) -> bool:
    try:
        metadata = json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(metadata, dict) and metadata.get("source_commit") == SOURCE_COMMIT


def check_managed_destination(root: Path) -> Path:
    root = root.expanduser().resolve()
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError(
            "请选择一个专门用于 IndexTTS 的子目录，不要选择磁盘根目录或用户主目录。"
        )
    if root.exists():
        if not root.is_dir():
            raise ValueError("安装位置不是文件夹。")
        if not is_managed_engine(root):
            raise ValueError("该目录已存在且不是本应用创建的引擎；请选一个新目录。")
    else:
        existing_parent = root.parent
        while (
            not existing_parent.exists() and existing_parent != existing_parent.parent
        ):
            existing_parent = existing_parent.parent
        if not existing_parent.is_dir():
            raise ValueError("安装位置所在的磁盘不可用。")
        if shutil.disk_usage(existing_parent).free < 25 * 1024**3:
            raise ValueError(
                "首次安装建议至少 25 GB 空间，所选磁盘空间不足；请换一个位置。"
            )
    return root


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise InstallCancelled("安装已取消；再次启动向导可继续下载未完成的模型。")


def _download_source(
    archive: Path, cancel: threading.Event, progress: Progress
) -> None:
    _check_cancel(cancel)
    if archive.is_file() and _sha256(archive) == SOURCE_SHA256:
        progress("已找到校验通过的 IndexTTS 源码包。")
        return
    archive.parent.mkdir(parents=True, exist_ok=True)
    bundled = (
        Path(sys.executable).parent / "engine-source" / f"index-tts-{SOURCE_COMMIT}.zip"
    )
    if bundled.is_file() and _sha256(bundled) == SOURCE_SHA256:
        progress("使用安装包内已校验的 IndexTTS 源码，无需连接 GitHub。")
        shutil.copy2(bundled, archive)
        return
    partial = archive.with_suffix(".part")
    progress("正在从 IndexTTS 官方仓库下载源码…")
    try:
        with (
            urllib.request.urlopen(SOURCE_URL, timeout=45) as response,
            partial.open("wb") as out,
        ):
            digest = hashlib.sha256()
            total = 0
            last_update = 0.0
            while chunk := response.read(1024 * 1024):
                _check_cancel(cancel)
                out.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                if time.monotonic() - last_update > 1:
                    progress(f"正在下载源码：{total / 1048576:.0f} MB")
                    last_update = time.monotonic()
        if digest.hexdigest() != SOURCE_SHA256:
            raise RuntimeError("源码校验失败，请检查网络连接后重试。")
        partial.replace(archive)
    finally:
        partial.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_source(archive: Path, root: Path, cancel: threading.Event) -> None:
    prefix = f"index-tts-{SOURCE_COMMIT}/"
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".voice-app-source-", dir=root.parent
    ) as temp:
        stage = Path(temp) / "engine"
        stage.mkdir()
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                _check_cancel(cancel)
                if not member.filename.startswith(prefix):
                    raise RuntimeError("源码包目录结构不符合预期。")
                if "\\" in member.filename or ":" in member.filename:
                    raise RuntimeError("源码包包含不安全的路径。")
                relative = PurePosixPath(member.filename[len(prefix) :])
                if not relative.parts or member.is_dir():
                    continue
                if ".." in relative.parts or relative.is_absolute():
                    raise RuntimeError("源码包包含不安全的路径。")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("源码包包含不支持的符号链接。")
                target = stage.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise RuntimeError("源码包包含不安全的路径。")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not (stage / "indextts" / "infer_v2_5.py").is_file():
            raise RuntimeError("源码包中缺少 IndexTTS 2.5 推理文件。")
        (stage / MARKER).write_text(
            json.dumps(
                {"source_commit": SOURCE_COMMIT, "model_revision": MODEL_REVISION}
            ),
            encoding="utf-8",
        )
        _check_cancel(cancel)
        # Never merge downloaded code into a pre-existing, unmarked directory.
        if root.exists():
            raise ValueError("安装目录已出现，请选择新目录后重试。")
        stage.replace(root)


def _run_command(
    command: list[str],
    root: Path,
    environment: dict[str, str],
    log_path: Path,
    cancel: threading.Event,
    progress: Progress,
) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write("\n$ " + " ".join(command) + "\n")
        log.flush()
        output_start = log.tell()
        with external_program():
            process = subprocess.Popen(
                command,
                cwd=root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        started = time.monotonic()
        last_report = started
        while True:
            if cancel.is_set():
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                raise InstallCancelled("安装已取消；重新运行向导可继续。")
            try:
                code = process.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - last_report >= 3:
                    with log_path.open("rb") as tail:
                        tail.seek(max(output_start, log_path.stat().st_size - 2048))
                        lines = (
                            tail.read().decode("utf-8", errors="replace").splitlines()
                        )
                    detail = lines[-1][:180] if lines else "正在安装，请稍候…"
                    elapsed = round(time.monotonic() - started)
                    progress(f"已运行 {elapsed} 秒 · {detail}")
                    last_report = time.monotonic()
    if code:
        with log_path.open("rb") as tail:
            tail.seek(max(output_start, log_path.stat().st_size - 4096))
            lines = tail.read().decode("utf-8", errors="replace").splitlines()
        useful = [
            line.strip()
            for line in lines
            if line.strip().startswith(("error:", "cause:", "Caused by:"))
        ]
        detail = "\n".join(useful[-5:] or lines[-8:])[:1100] or "这一步未完成，请重试。"
        raise InstallCommandError(code, detail, log_path)
    progress("这一步已完成。")


def _physical_python(root: Path) -> Path:
    return (
        root
        / ".uv-python"
        / f"cpython-{PYTHON_VERSION}-windows-x86_64-none"
        / "python.exe"
    )


def _python_is_usable(python: Path, environment: dict[str, str]) -> bool:
    try:
        # Never accept a linked directory as the supposedly physical runtime.
        for path in (python, python.parent, python.parent.parent):
            if getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                return False
        with external_program():
            result = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import json,sys,struct; "
                    "print(json.dumps([list(sys.version_info[:3]),struct.calcsize('P')*8]))",
                ],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        expected = [[int(part) for part in PYTHON_VERSION.split(".")], 64]
        return result.returncode == 0 and json.loads(result.stdout) == expected
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _ensure_python(root, uv, environment, log_path, cancel, progress) -> Path:
    """Use the full patch-version path, not uv's Windows minor-version junction."""
    _check_cancel(cancel)
    python = _physical_python(root)
    if _python_is_usable(python, environment):
        progress("已验证独立 Python，直接使用实际目录，无需重新下载。")
        return python
    progress(f"正在自动准备独立 Python {PYTHON_VERSION}…")
    try:
        command = [uv, "python", "install", PYTHON_VERSION, "--no-bin", "--no-registry"]
        if python.parent.exists():
            command.append("--reinstall")
        _run_command(
            command,
            root,
            environment,
            log_path,
            cancel,
            progress,
        )
    except InstallCommandError as exc:
        link_failure = (
            "os error 448" in exc.detail or "minor version link" in exc.detail
        )
        if not link_failure or not _python_is_usable(python, environment):
            raise
        progress("Python 本体已就绪；检测到 Windows 目录链接限制，改用实际目录。")
    _check_cancel(cancel)
    if not _python_is_usable(python, environment):
        raise RuntimeError(
            "Python 安装未通过版本与运行检查，请重试；不会进入生成界面。"
        )
    return python


def _ensure_app_environment(root, base_python, environment, log_path, cancel, progress):
    """Repair only our launchers, retaining installed packages and legacy .venv.

    uv's upgradeable Windows environments can embed the minor-version junction
    even when --python names a physical executable. CPython venv with copies
    instead binds both the launcher and pyvenv.cfg to the pinned physical Python.
    Never use --clear here: this is also the resume path after a failed install.
    """
    _check_cancel(cancel)
    target = root / APP_ENVIRONMENT
    # Refuse redirects instead of overwriting an environment outside the engine.
    for path in (
        target,
        target / "Scripts",
        target / "Lib",
        target / "Lib" / "site-packages",
        target / "Include",
        target / "pyvenv.cfg",
        target / "Scripts" / "python.exe",
        target / "Scripts" / "pythonw.exe",
    ):
        try:
            linked = (
                path.is_symlink()
                or getattr(path.lstat(), "st_file_attributes", 0) & 0x400
            )
        except FileNotFoundError:
            linked = False
        if linked:
            raise RuntimeError("应用推理环境包含目录链接，请选择新的专用引擎目录。")
    progress("准备应用专用 Python 启动器，保留已有依赖与模型…")
    _run_command(
        [
            str(base_python),
            "-I",
            "-B",
            "-m",
            "venv",
            "--without-pip",
            "--copies",
            str(target),
        ],
        root,
        environment,
        log_path,
        cancel,
        progress,
    )
    python = target / "Scripts" / "python.exe"
    _verify_app_python(
        root, python, base_python, environment, log_path, cancel, progress
    )
    return python


def _verify_app_python(
    root, python, base_python, environment, log_path, cancel, progress
):
    # Probe the actual launcher, not merely the existence of python.exe. This
    # runs before GPU checks so a Python startup failure is not called a GPU error.
    script = (
        "import os,sys,struct; "
        "assert tuple(sys.version_info[:3]) == "
        f"{tuple(map(int, PYTHON_VERSION.split('.')))!r}; "
        "assert struct.calcsize('P')*8 == 64; "
        "assert os.path.normcase(sys.prefix) == "
        f"os.path.normcase({str(python.parent.parent)!r}); "
        "assert os.path.normcase(sys._base_executable) == "
        f"os.path.normcase({str(base_python)!r}), "
        "'Python launcher still uses a directory link'; "
        "print('Application Python self-test OK:',sys.executable,sys._base_executable)"
    )
    _run_command(
        [str(python), "-I", "-B", "-c", script],
        root,
        environment,
        log_path,
        cancel,
        progress,
    )


def _invalid_model_files(checkpoints: Path) -> list[str]:
    invalid = []
    for name, expected_sha256 in MODEL_SHA256.items():
        path = checkpoints / name
        if not path.is_file() or _sha256(path) != expected_sha256:
            invalid.append(name)
    return invalid


def _download_models(
    root: Path,
    uv: str,
    environment: dict[str, str],
    log_path: Path,
    cancel: threading.Event,
    progress: Progress,
    source: str,
) -> None:
    del uv, environment  # Downloads do not need a separately installed CLI.
    for item in PRIMARY_MODELS:
        download_file(
            item, root / "checkpoints" / item.path, source, progress, cancel, log_path
        )


def _download_auxiliary_models(root, source, progress, cancel, log_path):
    for item in AUXILIARY_MODELS:
        download_file(
            item, root / "checkpoints" / item.path, source, progress, cancel, log_path
        )


def _check_compute(root, python, environment, log_path, cancel, progress):
    progress("[2/5] 检查 NVIDIA 显卡和 CUDA 推理能力…")
    script = (
        "import torch; "
        "assert torch.cuda.is_available(), "
        "'未检测到可用 NVIDIA CUDA 显卡，请确认驱动可用'; "
        "x=torch.ones(4,device='cuda'); assert x.sum().item()==4; "
        "print('CUDA self-test OK:',torch.cuda.get_device_name(0),torch.__version__)"
    )
    try:
        _run_command(
            [str(python), "-c", script], root, environment, log_path, cancel, progress
        )
    except DownloadCancelled:
        raise
    except RuntimeError as exc:
        raise RuntimeError(
            "显卡自检未通过。本版本需要可用的 NVIDIA CUDA 显卡和驱动；"
            "Python 与 CUDA 推理库由程序安装，无需自行配置。\n" + str(exc)
        ) from exc


def _prepare_files(
    root, python, uv, backend_source, environment, log_path, cancel, progress, source
):
    _check_compute(root, python, environment, log_path, cancel, progress)
    missing = model_inventory_errors(root)
    required = sum(item.size for item in ALL_MODELS if item.path in missing)
    if missing and shutil.disk_usage(root).free < required + 2 * 1024**3:
        raise ValueError(
            "剩余空间不足以完成模型安装，请释放空间或选择另一磁盘；已下载文件会保留。"
        )
    progress("[3/5] 准备全部语音模型（含辅助模型，共约 7.1 GB）…")
    _download_models(root, uv, environment, log_path, cancel, progress, source)
    _download_auxiliary_models(root, source, progress, cancel, log_path)
    _check_cancel(cancel)
    progress("[4/5] 安装 Voice App 后端…")
    constraints = Path(sys.executable).parent / "backend-constraints.txt"
    if not constraints.is_file():
        constraints = (
            Path(__file__).resolve().parent.parent / "packaging" / constraints.name
        )
    if not constraints.is_file():
        raise RuntimeError("安装包缺少锁定的后端依赖清单，请重新获取完整安装包。")
    _run_command(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-managed-python",
            "--no-python-downloads",
            "--constraint",
            # uv splits constraint arguments on spaces; percent-encoded file
            # URIs keep normal Windows installation paths intact.
            constraints.resolve().as_uri(),
            *backend_source,
        ],
        root,
        environment,
        log_path,
        cancel,
        progress,
    )
    _check_cancel(cancel)
    receipt = root / ".voice-app-prepared.json"
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps({"manifest": MANIFEST_VERSION}), encoding="utf-8")
    temporary.replace(receipt)
    progress("[5/5] 文件准备完成，接下来启动模型进行真实自检…")


def prepare_existing_engine(
    root, data_dir, uv, backend_source, progress, cancel, model_source="auto"
):
    """Repair model/dependency files without replacing user-provided source."""
    python = engine_python(root)
    if not python.is_file() or not (root / "indextts" / "infer_v2_5.py").is_file():
        raise ValueError("已有引擎缺少源码或 Python，请选择自动安装。")
    data_dir.mkdir(parents=True, exist_ok=True)
    _prepare_files(
        root,
        python,
        uv,
        backend_source,
        engine_environment(root, data_dir),
        data_dir / "setup.log",
        cancel,
        progress,
        model_source,
    )
    return root, python


def bundle_source(folder: Path) -> Path:
    """Ship the small pinned source archive so first use does not need GitHub."""
    archive = folder / f"index-tts-{SOURCE_COMMIT}.zip"
    cached = (
        Path(__file__).resolve().parent.parent
        / "build"
        / f"indextts-source-{SOURCE_COMMIT[:7]}.zip"
    )
    folder.mkdir(parents=True, exist_ok=True)
    if cached.is_file() and _sha256(cached) == SOURCE_SHA256:
        shutil.copy2(cached, archive)
    else:
        _download_source(archive, threading.Event(), print)
    return archive


def install_managed_engine(
    destination: Path,
    data_dir: Path,
    uv: str,
    backend_source: list[str],
    progress: Progress,
    cancel: threading.Event,
    model_source: str = "auto",
) -> tuple[Path, Path]:
    """Install or resume a managed engine; never touch an unrelated installation."""
    _check_cancel(cancel)
    root = check_managed_destination(destination)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "setup.log"
    if not is_managed_engine(root):
        archive = data_dir / "downloads" / f"index-tts-{SOURCE_COMMIT}.zip"
        _download_source(archive, cancel, progress)
        progress("正在解压并验证 IndexTTS 源码…")
        _extract_source(archive, root, cancel)
    _check_cancel(cancel)
    environment = engine_environment(root, data_dir)

    progress("[1/5] 自动安装 Python 与 CUDA 推理依赖；无需自行配置环境…")
    try:
        base_python = _ensure_python(root, uv, environment, log_path, cancel, progress)
        python = _ensure_app_environment(
            root, base_python, environment, log_path, cancel, progress
        )
        _run_command(
            [
                uv,
                "sync",
                "--locked",
                "--no-dev",
                "--python",
                str(python),
                "--no-python-downloads",
            ],
            root,
            environment,
            log_path,
            cancel,
            progress,
        )
        _verify_app_python(
            root, python, base_python, environment, log_path, cancel, progress
        )
    except DownloadCancelled:
        raise
    except RuntimeError as exc:
        raise RuntimeError(f"Python 与 IndexTTS 依赖安装失败：{exc}") from exc
    python = root / APP_ENVIRONMENT / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError("依赖安装结束，但未找到 IndexTTS 的 Python 环境。")

    _prepare_files(
        root,
        python,
        uv,
        backend_source,
        environment,
        log_path,
        cancel,
        progress,
        model_source,
    )
    return root, python


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-source", type=Path, required=True)
    args = parser.parse_args()
    bundle_source(args.bundle_source)
