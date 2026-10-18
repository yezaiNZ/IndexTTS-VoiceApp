"""Launch an independent runtime without inheriting the frozen UI's DLL path."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from contextlib import contextmanager

_launch_lock = threading.RLock()


@contextmanager
def external_program():
    """Temporarily undo PyInstaller's Windows DLL override for child creation."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    with _launch_lock:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_directory = kernel.GetDllDirectoryW
        get_directory.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
        get_directory.restype = ctypes.c_uint32
        set_directory = kernel.SetDllDirectoryW
        set_directory.argtypes = [ctypes.c_wchar_p]
        set_directory.restype = ctypes.c_int
        length = get_directory(0, None)
        buffer = ctypes.create_unicode_buffer(length + 1)
        get_directory(len(buffer), buffer)
        previous = buffer.value or None
        if not set_directory(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not set_directory(previous):
                raise ctypes.WinError(ctypes.get_last_error())


def independent_environment(environment: dict[str, str]) -> dict[str, str]:
    """Do not import UI/site overrides into the separately installed Python."""
    result = dict(environment)
    for name in (
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "UV_MANAGED_PYTHON",
        "UV_NO_MANAGED_PYTHON",
        "UV_PYTHON_PREFERENCE",
    ):
        result.pop(name, None)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundle = os.path.normcase(os.path.abspath(sys._MEIPASS))

        def bundled_path(value):
            candidate = os.path.normcase(os.path.abspath(value))
            return candidate == bundle or candidate.startswith(bundle + os.sep)

        result["PATH"] = os.pathsep.join(
            value
            for value in result.get("PATH", "").split(os.pathsep)
            if value and not bundled_path(value)
        )
    return result
