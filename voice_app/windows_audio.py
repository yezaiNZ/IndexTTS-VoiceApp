"""Read the current Windows multimedia defaults without opening audio streams."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_WAVE_MAPPER = 0xFFFFFFFF
_DRVM_MAPPER_PREFERRED_GET = 0x2000 + 21
_MAPPER_NAMES = (
    "sound mapper",
    "声音映射器",
    "primary sound driver",
    "主声音驱动",
    "primary sound capture driver",
    "主声音捕获驱动",
)


class _WaveInCaps(ctypes.Structure):
    _fields_ = [
        ("manufacturer", wintypes.WORD),
        ("product", wintypes.WORD),
        ("driver_version", wintypes.UINT),
        ("name", wintypes.WCHAR * 32),
        ("formats", wintypes.DWORD),
        ("channels", wintypes.WORD),
        ("reserved", wintypes.WORD),
    ]


class _WaveOutCaps(_WaveInCaps):
    _fields_ = [("support", wintypes.DWORD)]


def is_device_mapper(name: str) -> bool:
    return any(part in name.casefold() for part in _MAPPER_NAMES)


def preferred_wave_name(*, input_device: bool) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        winmm = ctypes.WinDLL("winmm.dll")
        message = winmm.waveInMessage if input_device else winmm.waveOutMessage
        message.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        message.restype = wintypes.UINT
        preferred = wintypes.DWORD(_WAVE_MAPPER)
        flags = wintypes.DWORD()
        if (
            message(
                _WAVE_MAPPER,
                _DRVM_MAPPER_PREFERRED_GET,
                ctypes.byref(preferred),
                ctypes.byref(flags),
            )
            or preferred.value == _WAVE_MAPPER
        ):
            return None
        caps = _WaveInCaps() if input_device else _WaveOutCaps()
        get_caps = winmm.waveInGetDevCapsW if input_device else winmm.waveOutGetDevCapsW
        get_caps.argtypes = [ctypes.c_size_t, ctypes.c_void_p, wintypes.UINT]
        get_caps.restype = wintypes.UINT
        if get_caps(preferred.value, ctypes.byref(caps), ctypes.sizeof(caps)):
            return None
        return caps.name or None
    except (OSError, AttributeError):
        return None
