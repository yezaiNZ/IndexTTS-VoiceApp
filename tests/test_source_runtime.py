import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_app import source_runtime as setup


@pytest.fixture
def source(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        'desktop = ["PySide6-Essentials==6.11.2"]\n'
    )
    python = (
        tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.touch()
    return tmp_path, python


def test_healthy_qt_never_runs_package_install(source, monkeypatch):
    root, python = source
    monkeypatch.setattr(setup, "probe_qt", lambda *args: (True, "healthy"))
    monkeypatch.setattr(
        setup.shutil, "which", lambda *args: pytest.fail("No installer needed")
    )
    assert setup.ensure_qt(root, python, repair=True, progress=lambda _: None)


def test_broken_qt_check_only_does_not_modify_environment(source, monkeypatch):
    root, python = source
    monkeypatch.setattr(setup, "probe_qt", lambda *args: (False, "missing initializer"))
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *a, **kw: pytest.fail("Must not install")
    )
    assert not setup.ensure_qt(root, python, progress=lambda _: None)


def test_repair_restores_overlapping_package_files_and_rechecks_real_import(
    source, monkeypatch
):
    root, python = source
    checks = iter(
        [
            (False, "PySide6 initializer missing after meta-package removal"),
            (True, "loaded"),
        ]
    )
    monkeypatch.setattr(setup, "probe_qt", lambda *args: next(checks))
    monkeypatch.setattr(setup.shutil, "which", lambda _: "uv.exe")
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.ensure_qt(root, python, repair=True, progress=lambda _: None)
    command, options = commands[0]
    assert command.count("--reinstall-package") == 2
    assert "--no-deps" in command and "--reinstall" not in command
    assert command[-2:] == ["PySide6-Essentials==6.11.2", "shiboken6==6.11.2"]
    assert command[command.index("--python") + 1] == str(python)
    assert options["env"]["UV_CACHE_DIR"] == str(root / ".uv-cache")
    assert all("torch" not in item and "checkpoints" not in item for item in command)


def test_pip_success_is_not_enough_if_qt_still_cannot_import(source, monkeypatch):
    root, python = source
    monkeypatch.setattr(setup, "probe_qt", lambda *args: (False, "DLL load failed"))
    monkeypatch.setattr(setup.shutil, "which", lambda _: "uv.exe")
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    assert not setup.ensure_qt(root, python, repair=True, progress=lambda _: None)


def test_repair_will_not_target_a_global_python(source, monkeypatch):
    root, _ = source
    monkeypatch.setattr(setup, "probe_qt", lambda *args: (False, "bad"))
    monkeypatch.setattr(setup.shutil, "which", lambda _: pytest.fail("Unsafe repair"))
    assert not setup.ensure_qt(
        root, root / "global-python.exe", repair=True, progress=lambda _: None
    )


def test_missing_uv_does_not_trigger_unknown_dll_downloads(source, monkeypatch):
    root, python = source
    monkeypatch.setattr(setup, "probe_qt", lambda *args: (False, "bad"))
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    assert not setup.ensure_qt(root, python, repair=True, progress=lambda _: None)


@pytest.mark.parametrize(
    "error", [OSError("offline"), setup.subprocess.TimeoutExpired("uv", 180)]
)
def test_repair_failure_is_reported_without_false_success(source, monkeypatch, error):
    root, python = source
    monkeypatch.setattr(setup, "probe_qt", lambda *args: (False, "bad"))
    monkeypatch.setattr(setup.shutil, "which", lambda _: "uv.exe")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(setup.subprocess, "run", fail)
    assert not setup.ensure_qt(root, python, repair=True, progress=lambda _: None)


def test_probe_checks_dll_imports_and_all_three_versions(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"qt": "6.11.2", "pyside": "6.11.2", "shiboken": "6.10.0"}
            ),
        )

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert not setup.probe_qt(Path("python.exe"), "6.11.2")[0]
    assert "-I" in commands[0] and "QtWidgets" in commands[0][-1]


def test_repository_qt_really_imports_in_fresh_process():
    root = Path(__file__).resolve().parents[1]
    ready, detail = setup.probe_qt(Path(sys.executable), setup.pinned_qt_version(root))
    assert ready, detail


def test_both_source_scripts_check_qt_before_claiming_success():
    root = Path(__file__).resolve().parents[1]
    start = (root / "start_voice_app.bat").read_text()
    installer = (root / "setup_voice_app.bat").read_text()
    assert start.index("voice_app.source_runtime --repair") < start.index(
        "-m voice_app desktop"
    )
    assert installer.index("uv sync") < installer.index(
        "voice_app.source_runtime --repair"
    )
    assert installer.index("voice_app.source_runtime --repair") < installer.index(
        "environment is ready"
    )
