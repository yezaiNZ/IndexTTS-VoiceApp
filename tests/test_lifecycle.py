import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_app import __main__ as cli
from voice_app import lifecycle
from voice_app.client_main import _stop_backend


def test_standalone_server_has_no_desktop_owner():
    assert lifecycle.watch_desktop_owner() is None


@pytest.mark.parametrize(
    "arguments", [(10, None, None), (10, 5, None), (None, 5, "event"), (-1, 5, "event")]
)
def test_incomplete_owner_parameters_are_rejected(arguments):
    with pytest.raises(ValueError, match="Incomplete"):
        lifecycle.watch_desktop_owner(*arguments)


def test_server_establishes_owner_before_loading_api_and_closes_it(monkeypatch):
    calls = []
    guard = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(
        lifecycle, "watch_desktop_owner", lambda *args: (calls.append(args), guard)[1]
    )
    monkeypatch.setattr(cli, "_run_server", lambda args: (calls.append("run"), 0)[1])
    assert (
        cli.main(
            [
                "server",
                "--index-tts-root",
                "test",
                "--desktop-owner-pid",
                "10",
                "--desktop-owner-created",
                "20",
                "--desktop-start-event",
                "event",
            ]
        )
        == 0
    )
    assert calls == [(10, 20, "event"), "run", "close"]


def test_server_ownership_is_closed_on_startup_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lifecycle,
        "watch_desktop_owner",
        lambda *args: SimpleNamespace(close=lambda: calls.append("close")),
    )

    def fail(args):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(cli, "_run_server", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        cli.main(["server", "--index-tts-root", "test"])
    assert calls == ["close"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_windows_job_structure_matches_64bit_sdk_layout():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(lifecycle._ExtendedLimits) == 144


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_parent_identity_cannot_be_replaced_by_recycled_pid():
    api = lifecycle._Windows()
    wrong_created = api.creation_time(api.GetCurrentProcess()) + 1
    with pytest.raises(RuntimeError, match="identity changed"):
        lifecycle.ParentWatch(os.getpid(), wrong_created, "unused-event")


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_owned_backend_cannot_start_without_the_private_startup_event():
    api = lifecycle._Windows()
    created = api.creation_time(api.GetCurrentProcess())
    with pytest.raises(OSError):
        lifecycle.ParentWatch(
            os.getpid(), created, "Local\\IndexTTSVoiceApp.Missing.Test.Event"
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_job_kills_its_process_and_does_not_touch_unrelated_process(tmp_path):
    owner = lifecycle.BackendOwnership()
    flags = subprocess.CREATE_NO_WINDOW
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"], creationflags=flags
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"], creationflags=flags
    )
    try:
        owner.attach(child)
        owner.close()
        child.wait(timeout=10)
        assert unrelated.poll() is None
        owner.close()  # Idempotent normal cleanup / finally blocks.
    finally:
        owner.close()
        for process in (child, unrelated):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_owner_handles_are_private_and_startup_waits_for_job_attachment(tmp_path):
    owner = lifecycle.BackendOwnership()
    assert not os.get_handle_inheritable(owner.job)
    assert not os.get_handle_inheritable(owner.gate)
    ready = tmp_path / "started"
    project = str(Path(__file__).resolve().parent.parent)
    code = (
        "import sys,time;from pathlib import Path;"
        f"sys.path.insert(0,{project!r});"
        "from voice_app.lifecycle import watch_desktop_owner;"
        "guard=watch_desktop_owner(int(sys.argv[1]),int(sys.argv[2]),sys.argv[3]);"
        f"Path({str(ready)!r}).touch();time.sleep(30)"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(os.getpid()),
            str(owner.parent_created),
            owner.event_name,
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        time.sleep(0.25)
        assert child.poll() is None and not ready.exists()
        owner.attach(child)
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        owner.close()
        child.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
def test_parent_watch_exits_when_owner_exits_without_a_job(tmp_path):
    # Isolate the fallback: the child deliberately isn't assigned to a job.
    # It must still die on its parent's process handle, not a PID-name lookup.
    ready = tmp_path / "ready.json"
    project = str(Path(__file__).resolve().parent.parent)
    child_code = (
        "import sys,time,json,os;from pathlib import Path;"
        f"sys.path.insert(0,{project!r});"
        "from voice_app.lifecycle import watch_desktop_owner;"
        "watch=watch_desktop_owner(int(sys.argv[1]),int(sys.argv[2]),sys.argv[3]);"
        f"Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid()}}));"
        "time.sleep(30)"
    )
    parent_code = (
        "import sys,time,subprocess;"
        f"sys.path.insert(0,{project!r});"
        "from voice_app.lifecycle import BackendOwnership;"
        "owner=BackendOwnership();"
        "owner.api.SetEvent(owner.gate);"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r},str(__import__('os').getpid()),str(owner.parent_created),owner.event_name],creationflags=subprocess.CREATE_NO_WINDOW);"
        "time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], creationflags=subprocess.CREATE_NO_WINDOW
    )
    child_handle = None
    api = lifecycle._Windows()
    try:
        deadline = time.monotonic() + 15
        while not ready.is_file():
            assert parent.poll() is None
            assert time.monotonic() < deadline, "Watch child didn't start"
            time.sleep(0.05)
        child_pid = json.loads(ready.read_text())["pid"]
        child_handle = api.required(api.OpenProcess(0x100000, False, child_pid))
        parent.terminate()
        parent.wait(timeout=5)
        assert api.wait([child_handle], 10000) == 0
    finally:
        if parent.poll() is None:
            parent.terminate()
        parent.wait(timeout=5)
        if child_handle:
            api.CloseHandle(child_handle)


def test_stop_backend_closes_ownership_even_if_launcher_already_exited(tmp_path):
    calls = []
    process = SimpleNamespace(
        _voice_app_ownership=SimpleNamespace(close=lambda: calls.append("owned-close")),
        poll=lambda: 0,
    )
    log = (tmp_path / "log").open("w")
    _stop_backend(process, log)
    assert calls == ["owned-close"] and log.closed
