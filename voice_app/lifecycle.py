"""Windows ownership for desktop-started backends, including abnormal UI exit.

The UI alone holds a non-inheritable kill-on-close Job handle. A startup event
prevents the backend serving requests before it is assigned. An independent
parent-process handle watch covers the short pre-assignment startup race and
interpreters whose Windows launcher has already spawned its own child.
Standalone servers do not opt into this ownership contract.
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Windows process ownership is unavailable")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": (
                [wintypes.HANDLE, wintypes.HANDLE],
                wintypes.BOOL,
            ),
            "CreateEventW": (
                [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
                wintypes.HANDLE,
            ),
            "OpenEventW": (
                [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR],
                wintypes.HANDLE,
            ),
            "SetEvent": ([wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": (
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "GetProcessTimes": (
                [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4,
                wintypes.BOOL,
            ),
            "WaitForMultipleObjects": (
                [
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.HANDLE),
                    wintypes.BOOL,
                    wintypes.DWORD,
                ],
                wintypes.DWORD,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)

    @staticmethod
    def required(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def creation_time(self, process):
        values = [wintypes.FILETIME() for _ in range(4)]
        self.required(self.GetProcessTimes(process, *(ctypes.byref(v) for v in values)))
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def wait(self, handles, timeout=0xFFFFFFFF):
        array = (wintypes.HANDLE * len(handles))(*handles)
        result = self.WaitForMultipleObjects(len(handles), array, False, timeout)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result


class BackendOwnership:
    """Retained by Popen in the UI; OS closure on a crash kills owned children."""

    def __init__(self):
        self.api = _Windows()
        self.job = self.gate = None
        self.event_name = "Local\\IndexTTSVoiceApp.Start." + uuid.uuid4().hex
        try:
            self.parent_created = self.api.creation_time(self.api.GetCurrentProcess())
            self.job = self.api.required(self.api.CreateJobObjectW(None, None))
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            self.api.required(
                self.api.SetInformationJobObject(
                    self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
            # SECURITY_ATTRIBUTES=NULL means neither handle is inherited.
            self.gate = self.api.required(
                self.api.CreateEventW(None, True, False, self.event_name)
            )
        except BaseException:
            self.close()
            raise

    def arguments(self):
        return [
            "--desktop-owner-pid",
            str(os.getpid()),
            "--desktop-owner-created",
            str(self.parent_created),
            "--desktop-start-event",
            self.event_name,
        ]

    def attach(self, process):
        # Popen's Windows process handle is retained and identifies this exact
        # child even if the PID is subsequently recycled.
        self.api.required(
            self.api.AssignProcessToJobObject(self.job, int(process._handle))
        )
        self.api.required(self.api.SetEvent(self.gate))

    def close(self):
        for name in ("gate", "job"):
            handle = getattr(self, name, None)
            if handle:
                setattr(self, name, None)
                self.api.CloseHandle(handle)


class ParentWatch:
    """Backend-side parent identity/handle watch; terminates only this process."""

    def __init__(self, pid: int, created: int, event_name: str):
        self.api = _Windows()
        self.parent = self.stop_event = None
        self.thread = None
        try:
            self.parent = self.api.required(
                self.api.OpenProcess(0x100000 | 0x1000, False, pid)
            )
            if self.api.creation_time(self.parent) != created:
                raise RuntimeError("Desktop owner process identity changed")
            gate = self.api.required(self.api.OpenEventW(0x100000, False, event_name))
            try:
                # Parent first: if both are signalled, owner death wins.
                if self.api.wait([self.parent, gate], 15000) != 1:
                    raise RuntimeError(
                        "Desktop exited before backend protection was ready"
                    )
            finally:
                self.api.CloseHandle(gate)
            self.stop_event = self.api.required(
                self.api.CreateEventW(None, True, False, None)
            )
            thread = threading.Thread(
                target=self._run, daemon=True, name="desktop-owner-watch"
            )
            thread.start()
            self.thread = thread
        except BaseException:
            self.close()
            raise

    def _run(self):
        try:
            if self.api.wait([self.parent, self.stop_event]) == 0:
                os._exit(0)  # Includes busy inference/audio threads after owner crash.
        except OSError:
            os._exit(1)  # Never keep serving after losing ownership protection.

    def close(self):
        if self.thread is not None:
            self.api.SetEvent(self.stop_event)
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                return  # Do not close a handle while a Windows wait is pending.
            self.thread = None
        for name in ("stop_event", "parent"):
            handle = getattr(self, name, None)
            if handle:
                setattr(self, name, None)
                self.api.CloseHandle(handle)


def watch_desktop_owner(pid=None, created=None, event_name=None):
    if pid is None and created is None and event_name is None:
        return None  # A deliberately standalone API server has no UI owner.
    if not pid or pid <= 0 or not created or not event_name:
        raise ValueError("Incomplete desktop backend ownership parameters")
    return ParentWatch(pid, created, event_name)
