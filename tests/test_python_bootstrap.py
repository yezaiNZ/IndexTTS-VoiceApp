import json
import sys
import threading
from types import SimpleNamespace

import pytest

from voice_app import engine_setup as setup
from voice_app.client_main import validate_engine


def test_python_bootstrap_uses_physical_patch_path_without_querying_junction(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(setup, "_python_is_usable", lambda *args: True)
    monkeypatch.setattr(
        setup, "_run_command", lambda *args: pytest.fail("Must not reinstall")
    )
    python = setup._ensure_python(
        tmp_path, "uv", {}, tmp_path / "log", threading.Event(), lambda _: None
    )
    assert f"cpython-{setup.PYTHON_VERSION}-windows-x86_64-none" == python.parent.name
    assert "cpython-3.11-windows" not in str(python)


def test_error_448_is_recovered_only_after_python_body_is_verified(
    tmp_path, monkeypatch
):
    checks = iter([False, True, True])
    monkeypatch.setattr(setup, "_python_is_usable", lambda *args: next(checks))
    commands = []

    def run(command, *args):
        commands.append(command)
        raise setup.InstallCommandError(
            2,
            "error: Failed to create Python minor version link directory\n"
            "cause: untrusted mount point (os error 448)",
            tmp_path / "log",
        )

    monkeypatch.setattr(setup, "_run_command", run)
    progress = []
    result = setup._ensure_python(
        tmp_path, "uv", {}, tmp_path / "log", threading.Event(), progress.append
    )
    assert result == setup._physical_python(tmp_path)
    assert "--no-bin" in commands[0] and "--no-registry" in commands[0]
    assert any("实际目录" in message for message in progress)


@pytest.mark.parametrize(
    "reason,available",
    [
        ("error: download failed (os error 448)", False),
        ("error: network connection timed out", True),
    ],
)
def test_python_failure_cannot_be_falsely_accepted(
    tmp_path, monkeypatch, reason, available
):
    checks = iter([False, available])
    monkeypatch.setattr(setup, "_python_is_usable", lambda *args: next(checks))

    def run(*args):
        raise setup.InstallCommandError(2, reason, tmp_path / "log")

    monkeypatch.setattr(setup, "_run_command", run)
    with pytest.raises(setup.InstallCommandError):
        setup._ensure_python(
            tmp_path, "uv", {}, tmp_path / "log", threading.Event(), lambda _: None
        )


@pytest.mark.parametrize(
    "version,bits", [("3.11.16", 64), ("3.11.13", 64), ("3.11.16", 32)]
)
def test_python_metadata_requires_pinned_version_and_64bit(
    tmp_path, monkeypatch, version, bits
):
    python = setup._physical_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.touch()
    metadata = [[int(part) for part in version.split(".")], bits]
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(metadata)
        ),
    )
    assert setup._python_is_usable(python, {}) == (
        version == setup.PYTHON_VERSION and bits == 64
    )


def test_engine_environment_does_not_modify_existing_venv_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "foreign")
    monkeypatch.setenv("PYTHONHOME", "foreign")
    monkeypatch.setenv("UV_MANAGED_PYTHON", "1")
    monkeypatch.setenv("UV_NO_MANAGED_PYTHON", "1")
    monkeypatch.setenv("PYTHONPATH", "foreign")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "foreign")
    environment = setup.engine_environment(tmp_path)
    assert environment["UV_PROJECT_ENVIRONMENT"] == str(
        tmp_path / setup.APP_ENVIRONMENT
    )
    assert environment["UV_PYTHON_INSTALL_BIN"] == "0"
    assert environment["UV_PYTHON_INSTALL_REGISTRY"] == "0"
    assert "UV_PYTHON_PREFERENCE" not in environment
    assert not {"VIRTUAL_ENV", "PYTHONHOME", "UV_MANAGED_PYTHON"} & environment.keys()
    assert "UV_NO_MANAGED_PYTHON" not in environment
    assert not {"PYTHONPATH", "__PYVENV_LAUNCHER__"} & environment.keys()


def test_launcher_prefers_app_environment_and_preserves_legacy_environment(tmp_path):
    (tmp_path / "indextts").mkdir()
    (tmp_path / "indextts" / "infer_v2_5.py").touch()
    (tmp_path / "checkpoints").mkdir()
    (tmp_path / "checkpoints" / "config.yaml").touch()
    old = tmp_path / ".venv" / "Scripts" / "python.exe"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"existing Python")
    new = tmp_path / setup.APP_ENVIRONMENT / "Scripts" / "python.exe"
    new.parent.mkdir(parents=True)
    new.touch()
    assert validate_engine(tmp_path)[1] == new
    assert old.read_bytes() == b"existing Python"


def test_command_error_includes_the_actual_failure_not_only_exit_code(tmp_path):
    cancel = threading.Event()
    with pytest.raises(setup.InstallCommandError, match="os error 448"):
        setup._run_command(
            [
                sys.executable,
                "-c",
                "print('error: Failed to inspect interpreter'); "
                "print('  cause: untrusted mount point (os error 448)'); "
                "raise SystemExit(2)",
            ],
            tmp_path,
            dict(setup.os.environ),
            tmp_path / "log",
            cancel,
            lambda _: None,
        )


def test_cancel_during_python_download_does_not_turn_into_installation_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(setup, "_python_is_usable", lambda *args: False)

    def run(*args):
        raise setup.InstallCancelled("cancelled")

    monkeypatch.setattr(setup, "_run_command", run)
    with pytest.raises(setup.InstallCancelled):
        setup._ensure_python(
            tmp_path, "uv", {}, tmp_path / "log", threading.Event(), lambda _: None
        )


def test_app_environment_repairs_launchers_without_clearing_installed_packages(
    tmp_path, monkeypatch
):
    old = tmp_path / ".venv" / "pyvenv.cfg"
    old.parent.mkdir()
    old.write_text("user environment", encoding="utf-8")
    dependency = (
        tmp_path / setup.APP_ENVIRONMENT / "Lib" / "site-packages" / "installed.py"
    )
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"existing dependencies")
    calls = []
    monkeypatch.setattr(
        setup, "_run_command", lambda command, *args: calls.append(command)
    )
    base = setup._physical_python(tmp_path)
    python = setup._ensure_app_environment(
        tmp_path, base, {}, tmp_path / "log", threading.Event(), lambda _: None
    )
    assert calls[0][:7] == [
        str(base),
        "-I",
        "-B",
        "-m",
        "venv",
        "--without-pip",
        "--copies",
    ]
    assert "--clear" not in calls[0] and "--upgrade-deps" not in calls[0]
    assert calls[0][-1] == str(tmp_path / setup.APP_ENVIRONMENT)
    assert calls[1][0] == str(python)
    assert "sys._base_executable" in calls[1][-1]
    assert dependency.read_bytes() == b"existing dependencies"
    assert old.read_text(encoding="utf-8") == "user environment"


def test_app_python_probe_failure_does_not_become_a_cuda_failure(tmp_path, monkeypatch):
    def run(command, *args):
        if command[1:4] == ["-I", "-B", "-c"]:
            raise setup.InstallCommandError(
                1, "Python launcher failed", tmp_path / "log"
            )

    monkeypatch.setattr(setup, "_run_command", run)
    with pytest.raises(setup.InstallCommandError, match="Python launcher failed"):
        setup._ensure_app_environment(
            tmp_path,
            setup._physical_python(tmp_path),
            {},
            tmp_path / "log",
            threading.Event(),
            lambda _: None,
        )


def test_app_python_probe_uses_physical_runtime_and_app_prefix(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        setup, "_run_command", lambda command, *args: calls.append(command)
    )
    python = tmp_path / setup.APP_ENVIRONMENT / "Scripts" / "python.exe"
    base = setup._physical_python(tmp_path)
    setup._verify_app_python(
        tmp_path, python, base, {}, tmp_path / "log", threading.Event(), lambda _: None
    )
    assert repr(str(base)) in calls[0][-1]
    assert repr(str(python.parent.parent)) in calls[0][-1]
    assert "-I" in calls[0] and "-B" in calls[0]


def test_app_environment_cancellation_prevents_launcher_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        setup, "_run_command", lambda *args: pytest.fail("Must not modify launcher")
    )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(setup.InstallCancelled):
        setup._ensure_app_environment(
            tmp_path,
            setup._physical_python(tmp_path),
            {},
            tmp_path / "log",
            cancel,
            lambda _: None,
        )


def test_command_diagnostics_do_not_include_old_installation_failure(tmp_path):
    log = tmp_path / "log"
    log.write_text(
        "error: earlier failed interpreter (os error 448)\n", encoding="utf-8"
    )
    with pytest.raises(setup.InstallCommandError) as error:
        setup._run_command(
            [sys.executable, "-c", "raise ValueError('current failure')"],
            tmp_path,
            dict(setup.os.environ),
            log,
            threading.Event(),
            lambda _: None,
        )
    assert "os error 448" not in error.value.detail
    assert "ValueError: current failure" in error.value.detail


def test_backend_constraints_support_unicode_and_spaces_in_install_location(
    tmp_path, monkeypatch
):
    bundle = tmp_path / "打包验证 v0.4.2"
    bundle.mkdir()
    constraints = bundle / "backend-constraints.txt"
    constraints.write_text("fastapi==0.141.1\n", encoding="utf-8")
    monkeypatch.setattr(setup.sys, "executable", str(bundle / "frontend.exe"))
    monkeypatch.setattr(setup, "_check_compute", lambda *args: None)
    monkeypatch.setattr(setup, "model_inventory_errors", lambda *args: [])
    monkeypatch.setattr(setup, "_download_models", lambda *args: None)
    monkeypatch.setattr(setup, "_download_auxiliary_models", lambda *args: None)
    calls = []
    monkeypatch.setattr(
        setup, "_run_command", lambda command, *args: calls.append(command)
    )
    setup._prepare_files(
        tmp_path,
        tmp_path / "python.exe",
        "uv",
        ["backend.whl"],
        {},
        tmp_path / "log",
        threading.Event(),
        lambda _: None,
        "auto",
    )
    argument = calls[0][calls[0].index("--constraint") + 1]
    assert argument == constraints.as_uri()
    assert " " not in argument and "%20" in argument
