"""Opt-in, bounded generated-audio cache. Never traverse or clean user folders."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

AUTO_NAME = re.compile(r"tts-[0-9a-f]{32}\.wav\Z")
TEMP_NAME = re.compile(r"tts-[0-9a-f]{32}\.wav\.part\Z")
MARKER = ".voice-app-audio-cache"
MARKER_CONTENT = "IndexTTS Voice App generated audio cache v1\n"
MIB = 1024 * 1024
LAST_AUDIO_LIMIT = 64 * MIB


def write_wav_atomic(destination: Path, pcm: bytes):
    """User-chosen exports are not cache entries; never truncate an old export."""
    destination = Path(destination)
    if AUTO_NAME.fullmatch(destination.name.lower()):
        raise ValueError("该文件名格式用于自动缓存，请换一个名称，避免参与自动清理")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".voice-app-export-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with wave.open(stream, "wb") as writer:
                writer.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                writer.writeframes(pcm)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ordinary_file(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & 0x400
    )


class GeneratedAudioStore:
    def __init__(self, root: Path, limit_bytes: Callable[[], int] | None = None):
        self.root = Path(root).absolute()
        self.limit_bytes = limit_bytes or (lambda: 500 * MIB)
        self._lock = threading.RLock()
        self._results: OrderedDict[str, dict] = OrderedDict()

    def path_for(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("Invalid generated audio id")
        return self.root / f"tts-{job_id}.wav"

    def _prepare(self):
        if self.root.exists():
            info = self.root.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise OSError("自动保存目录不能是链接或重解析点")
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / MARKER
        if marker.exists():
            if (
                not _ordinary_file(marker)
                or marker.read_text(encoding="utf-8") != MARKER_CONTENT
            ):
                raise OSError("自动保存目录的归属标记无效，未执行清理")
        else:
            if any(self.root.iterdir()):
                raise OSError("自动保存目录已有其他文件，未执行清理")
            with marker.open("x", encoding="utf-8") as stream:
                stream.write(MARKER_CONTENT)

    def _prune(self, limit: int) -> int:
        # Called only after an explicitly requested save. No startup cleanup,
        # recursive deletion, legacy-output migration, or reference-audio cleanup.
        self._prepare()
        files = []
        for path in self.root.iterdir():
            try:
                if TEMP_NAME.fullmatch(path.name) and _ordinary_file(path):
                    if time.time() - path.stat().st_mtime > 86400:
                        # Crash leftovers only; current/locked writes are not touched.
                        path.unlink()
                    continue
                if AUTO_NAME.fullmatch(path.name) and _ordinary_file(path):
                    info = path.stat()
                    files.append((info.st_mtime_ns, path.name, path, info.st_size))
            except OSError:
                continue
        total = sum(item[3] for item in files)
        removed = 0
        for _, _, path, size in sorted(files):
            if total <= limit:
                break
            try:
                if not _ordinary_file(path):
                    continue
                path.unlink()
            except OSError:
                continue  # A locked file must not break live speech output.
            total -= size
            removed += 1
        return removed

    def begin(self, job_id: str, sample_rate: int) -> AudioRecording:
        return AudioRecording(self, job_id, sample_rate)

    def remember(self, job_id: str, result: dict):
        with self._lock:
            self._results[job_id] = {"job_id": job_id, **result}
            while len(self._results) > 64:
                self._results.popitem(last=False)

    def result(self, job_id: str) -> dict | None:
        with self._lock:
            found = self._results.get(job_id)
            result = dict(found) if found is not None else None
            if result and result.get("saved") and not Path(result["path"]).is_file():
                result.update(
                    saved=False, path=None, error="此文件已按保留策略清理或被移走"
                )
            return result


class AudioRecording:
    def __init__(self, store: GeneratedAudioStore, job_id: str, sample_rate: int):
        self.store, self.job_id = store, job_id
        self.path = store.path_for(job_id)
        self.temporary = self.path.with_suffix(".wav.part")
        self.writer = self.stream = None
        self.error = None
        self.bytes_written = 0
        self.limit = 0
        self.owns_temporary = False
        try:
            with store._lock:
                store._prepare()
                self.limit = int(store.limit_bytes())
                if self.limit < 44:
                    raise OSError("自动保存上限不足")
                # Held across streaming chunks; finish/discard always closes it.
                self.stream = self.temporary.open("xb")
                self.owns_temporary = True
                self.writer = wave.open(self.stream, "wb")  # noqa: SIM115
                self.writer.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        except (OSError, ValueError) as exc:
            self._discard(str(exc))

    def _close(self):
        try:
            if self.writer is not None:
                self.writer.close()
        finally:
            self.writer = None
            if self.stream is not None:
                self.stream.close()
                self.stream = None

    def _discard(self, error: str):
        self.error = error
        try:
            self._close()
        except OSError:
            pass
        if self.owns_temporary:
            try:
                self.temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def write(self, pcm: bytes):
        if self.error is not None:
            return
        if self.bytes_written + len(pcm) + 44 > self.limit:
            self._discard("本条音频超过自动保存上限，未保留文件；语音输出不受影响")
            return
        try:
            self.writer.writeframesraw(pcm)
            self.bytes_written += len(pcm)
        except OSError as exc:
            self._discard(f"自动保存失败：{exc}")

    def finish(self, complete: bool, *, playback_error: str | None = None) -> dict:
        removed = 0
        saved = False
        if not complete or self.bytes_written == 0:
            self._discard(self.error or "生成已停止或未完成，不保留残缺音频")
        elif self.error is None:
            try:
                self._close()
                with self.store._lock:
                    self.store._prepare()
                    if self.path.exists():
                        raise OSError("自动保存文件名已存在，不覆盖已有文件")
                    self.temporary.replace(self.path)
                    removed = self.store._prune(self.limit)
                    saved = self.path.is_file()
                    if not saved:
                        self.error = "保留上限内没有足够空间，本条音频未保留"
            except OSError as exc:
                self._discard(f"自动保存失败：{exc}")
        result = {
            "save_requested": True,
            "saved": saved,
            "path": str(self.path) if saved else None,
            "error": self.error,
            "removed_count": removed,
        }
        if playback_error:
            result["playback_error"] = playback_error
        self.store.remember(self.job_id, result)
        return result
