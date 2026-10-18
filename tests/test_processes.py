import os
from types import SimpleNamespace

import pytest

from voice_app import processes


def test_subprocess_environment_removes_python_overrides_without_mutating_parent():
    environment = {
        "PATH": "normal",
        "PYTHONPATH": "foreign",
        "PYTHONHOME": "foreign",
        "PYTHONEXECUTABLE": "ui.exe",
        "__PYVENV_LAUNCHER__": "ui.exe",
        "VIRTUAL_ENV": "foreign",
        "UV_PYTHON_PREFERENCE": "only-managed",
    }
    result = processes.independent_environment(environment)
    assert result == {"PATH": "normal"}
    assert environment["PYTHONHOME"] == "foreign"


def test_frozen_subprocess_path_excludes_bundle_but_preserves_other_entries(
    tmp_path, monkeypatch
):
    bundle = tmp_path / "_internal"
    other = tmp_path / "python"
    prefix_only = tmp_path / "_internal-other"
    monkeypatch.setattr(processes.sys, "frozen", True, raising=False)
    monkeypatch.setattr(processes.sys, "_MEIPASS", str(bundle), raising=False)
    result = processes.independent_environment(
        {
            "PATH": os.pathsep.join(
                map(str, (bundle, bundle / "PySide6", other, prefix_only))
            )
        }
    )
    assert result["PATH"] == os.pathsep.join(map(str, (other, prefix_only)))


def test_non_frozen_dll_context_does_not_call_windows_api(monkeypatch):
    monkeypatch.setattr(processes.sys, "frozen", False, raising=False)
    with processes.external_program():
        assert True


@pytest.mark.skipif(os.name != "nt", reason="Windows-only frozen launcher")
def test_frozen_dll_directory_is_restored_even_when_child_launch_fails(monkeypatch):
    calls = []

    class FakeGet:
        def __call__(self, size, buffer):
            if buffer is not None:
                buffer.value = "UI-bundle"
            return len("UI-bundle") + 1

    class FakeSet:
        def __call__(self, directory):
            calls.append(directory)
            return 1

    monkeypatch.setattr(processes.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        processes.ctypes,
        "WinDLL",
        lambda *args, **kwargs: SimpleNamespace(
            GetDllDirectoryW=FakeGet(), SetDllDirectoryW=FakeSet()
        ),
    )
    with pytest.raises(RuntimeError, match="child failed"):
        with processes.external_program():
            raise RuntimeError("child failed")
    assert calls == [None, "UI-bundle"]
