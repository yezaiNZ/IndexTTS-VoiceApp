"""Resumable, pinned, atomic downloads used by the desktop bootstrapper."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

import requests

from .model_manifest import ModelFile


class DownloadCancelled(RuntimeError):
    pass


def file_sha256(path: Path, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("安装已取消，已下载文件会保留。")
            digest.update(chunk)
    return digest.hexdigest()


def valid_file(path: Path, item: ModelFile, verify_hash: bool = True) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == item.size
        and (not verify_hash or file_sha256(path) == item.sha256)
    )


def download_urls(item: ModelFile, source: str) -> list[tuple[str, str]]:
    if source not in {"auto", "modelscope", "huggingface"}:
        raise ValueError("未知的模型下载来源。")
    remote = quote(item.remote_path, safe="/")
    hf = f"https://huggingface.co/{item.repo}/resolve/{item.revision}/{remote}"
    mirror = f"https://hf-mirror.com/{item.repo}/resolve/{item.revision}/{remote}"
    urls = []
    if source != "huggingface" and item.modelscope_repo:
        query = urlencode({"Revision": "master", "FilePath": item.remote_path})
        urls.append(
            (
                "ModelScope",
                f"https://modelscope.cn/api/v1/models/{item.modelscope_repo}/repo?{query}",
            )
        )
    urls.extend((("Hugging Face", hf), ("校验镜像", mirror)))
    return urls


def _quarantine_partial(path: Path) -> None:
    if path.exists():
        # Keep a recoverable copy rather than repeatedly resuming corrupt data.
        path.replace(path.with_name(path.name + f".invalid-{time.time_ns()}"))


def download_file(
    item: ModelFile,
    destination: Path,
    source: str,
    progress: Callable[[str], None],
    cancel: threading.Event,
    log_path: Path,
    urls: list[tuple[str, str]] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = destination.parent.resolve()
    if destination.is_symlink():
        raise ValueError(f"模型目标不能是符号链接：{destination}")
    if cancel.is_set():
        raise DownloadCancelled("安装已取消，已下载文件会保留。")
    progress(f"正在校验 {item.remote_path}…")
    if valid_file(destination, item):
        progress(f"已验证 {item.remote_path}，无需下载。")
        return
    partial = destination.with_name(destination.name + ".voice-app.part")
    if partial.is_symlink() or partial.resolve().parent != root:
        raise ValueError("下载临时路径不安全。")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    for label, url in urls if urls is not None else download_urls(item, source):
        for attempt in range(2):
            if cancel.is_set():
                raise DownloadCancelled("安装已取消，已下载文件会保留。")
            try:
                if partial.exists() and partial.stat().st_size >= item.size:
                    if valid_file(partial, item):
                        partial.replace(destination)
                        return
                    _quarantine_partial(partial)
                offset = partial.stat().st_size if partial.exists() else 0
                progress(
                    f"{label}：下载 {item.remote_path}，"
                    f"已完成 {offset / item.size:.0%}…"
                )
                headers = {
                    "Accept-Encoding": "identity",
                    "User-Agent": "IndexTTS-VoiceApp/0.4",
                }
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                with requests.get(
                    url, headers=headers, stream=True, timeout=(8, 20)
                ) as reply:
                    reply.raise_for_status()
                    if reply.status_code == 206:
                        prefix = f"bytes {offset}-"
                        if not reply.headers.get("Content-Range", "").startswith(
                            prefix
                        ):
                            raise RuntimeError("下载源返回了错误的续传位置。")
                    elif reply.status_code == 200:
                        offset = 0
                    elif reply.status_code != 206:
                        raise RuntimeError(f"不支持的下载响应：{reply.status_code}")
                    start = time.monotonic()
                    last_report = start
                    received = offset
                    with partial.open("ab" if offset else "wb") as out:
                        for chunk in reply.iter_content(chunk_size=1024 * 1024):
                            if cancel.is_set():
                                raise DownloadCancelled(
                                    "安装已取消，重新打开软件即可继续。"
                                )
                            if not chunk:
                                continue
                            received += len(chunk)
                            if received > item.size:
                                raise RuntimeError("下载内容超出已验证的模型大小。")
                            out.write(chunk)
                            now = time.monotonic()
                            if now - last_report >= 0.5:
                                speed = (
                                    (received - offset)
                                    / max(now - start, 0.01)
                                    / 1048576
                                )
                                progress(
                                    f"{item.remote_path}：{received / item.size:.1%} · "
                                    f"{received / 1048576:.0f}/"
                                    f"{item.size / 1048576:.0f} MB · {speed:.1f} MB/s"
                                )
                                last_report = now
                        out.flush()
                        os.fsync(out.fileno())
                if not valid_file(partial, item):
                    if partial.stat().st_size == item.size:
                        _quarantine_partial(partial)
                    raise RuntimeError("文件尚未完整或校验不符，将自动重试。")
                partial.replace(destination)
                progress(f"已完成并校验 {item.remote_path}。")
                return
            except DownloadCancelled:
                raise
            except Exception as exc:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(
                        f"\n下载失败 [{label}, attempt {attempt + 1}] {item.path}: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                failures.append(f"{label}: {type(exc).__name__}")
                progress(f"{item.remote_path} 下载暂时失败，正在自动重试或切换来源…")
    raise RuntimeError(
        f"未能完成 {item.remote_path} 的下载。"
        "请确认网络可用后点击重试，已下载内容会保留。"
        f"\n记录：{log_path}\n" + "、".join(dict.fromkeys(failures))
    )
