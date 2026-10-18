"""Opt-in REAL Inno upgrade tests, with an unrelated AppId and harmless payload.

VOICE_APP_INSTALLER_TESTS=1 enables this on Windows with build/inno/ISCC.exe.
Fixtures, logs and an evidence JSON remain under build for inspection. No real
Voice App installation, model, audio device or user-data directory is touched.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
TEST_ID = "{64FC1CE2-89FD-451C-B145-8CFBF7BCF6D0}"
TEST_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{TEST_ID}_is1"
PRODUCTION_ID = "{8256E0CB-4A91-4AC2-A955-174464D90A20}"


def test_installer_keeps_stable_id_and_separate_test_identity():
    source = (PROJECT / "packaging/installer.iss").read_text(encoding="utf-8-sig")
    assert TEST_ID in source and PRODUCTION_ID in source
    assert "DefaultDirName={code:DefaultInstallDirectory}" in source
    assert "UsePreviousAppDir=no" in source
    assert '#include "upgrade.iss"' in source
    upgrade = (PROJECT / "packaging/upgrade.iss").read_text(encoding="utf-8-sig")
    assert "DelTree(" not in upgrade
    assert (
        "UninstallString" not in upgrade
    )  # Never execute arbitrary registry commands.
    assert "VerifiedUninstaller(PreviousInstalls[I].Directory)" in upgrade
    assert "VoiceAppIsStopped" in upgrade and "VoicePortIsFree" in upgrade
    assert "Inno Setup Uninstall Log (b)" in upgrade


@pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("VOICE_APP_INSTALLER_TESTS") != "1",
    reason="Requires explicit opt-in for isolated Windows installer integration tests",
)
def test_real_windows_upgrade_retains_data_and_blocks_unsafe_operations():
    import winreg

    def registry_version():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
                return winreg.QueryValueEx(key, "DisplayVersion")[0]
        except FileNotFoundError:
            return None

    assert registry_version() is None, (
        "An earlier TEST installation needs cleanup first"
    )
    compiler = PROJECT / "build/inno/ISCC.exe"
    assert compiler.is_file()
    root = Path(tempfile.mkdtemp(prefix="upgrade test 中文 ", dir=PROJECT / "build"))
    payload = root / "payload"
    payload.mkdir()
    (payload / "IndexTTSVoiceApp.exe").write_bytes(b"Not executable; fixture only.")

    def payload_version(version):
        for wheel in payload.glob("*.whl"):
            wheel.unlink()  # Only generated fixture marker files, not installed files.
        (payload / f"index_tts_voice_app-{version}-py3-none-any.whl").write_text(
            "fixture"
        )

    def compile_installer(version, legacy=False):
        payload_version(version)
        script = "tests/legacy-fixture.iss" if legacy else "installer.iss"
        args = [str(compiler), f"/DBuildVersion={version}", f"/DUpgradeTestRoot={root}"]
        if not legacy:
            args.append("/DInstallerTest")
        args.append(str(PROJECT / "packaging" / script))
        result = subprocess.run(args, capture_output=True, timeout=30, check=False)
        (
            root / f"compile-{version}-{'legacy' if legacy else 'current'}.log"
        ).write_bytes(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout.decode(errors="replace")
        return (
            root
            / "packages"
            / f"{'Legacy' if legacy else 'Upgrade'}-Test-{version}.exe"
        )

    sequence = 0

    def run(exe, *args):
        nonlocal sequence
        sequence += 1
        return subprocess.run(
            [
                str(exe),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                f"/LOG={root / f'step-{sequence}.log'}",
                *args,
            ],
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        ).returncode

    def plan(exe, name):
        path = root / name
        # InitializeSetup returning False is Inno's exit code 1 (not cancellation 2).
        assert run(exe, f"/UPGRADEPLAN={path}") == 1
        return path.read_text(encoding="utf-8-sig")

    old_a = root / "IndexTTS Voice App v0.3.2"
    old_b = root / "IndexTTS Voice App v0.4.3"
    new = root / "IndexTTS Voice App v0.4.4"
    fresh = root / "IndexTTS Voice App v0.4.4 clean"
    installed = [old_a, old_b, new, fresh]
    checks = []
    try:
        # Create the legacy side-by-side layout: registration points only to B,
        # while A is discoverable only by its verified uninstall header.
        assert run(compile_installer("0.3.2", legacy=True), f"/DIR={old_a}") == 0
        assert run(compile_installer("0.4.3", legacy=True), f"/DIR={old_b}") == 0
        assert registry_version() == "0.4.3"
        assert (old_b / "unins000.dat").read_bytes()[64:102] == TEST_ID.encode()
        sentinels = {}
        for directory in (old_a, old_b, root / "IndexTTS-2.5-engine"):
            for name in (
                "presets/my-voice.wav",
                "outputs/settings.json",
                "checkpoints/model.bin",
            ):
                path = directory / name
                # Do not put checkpoints directly in the app root: that denotes
                # an engine and must intentionally be excluded from cleanup.
                if directory in (old_a, old_b) and name.startswith("checkpoints"):
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"PRIVATE DATA {directory.name} {name}".encode())
                sentinels[path] = path.read_bytes()
        fake = root / "IndexTTS Voice App portable"
        fake.mkdir()
        (fake / "IndexTTSVoiceApp.exe").write_bytes(b"portable fixture")
        source = root / "IndexTTS Voice App source"
        source.mkdir()
        (source / "pyproject.toml").write_text("source sentinel")
        installer = compile_installer("0.4.4")
        text = plan(installer, "before.txt")
        assert "count=2" in text and f"default={new}" in text
        assert str(old_a) in text and str(old_b) in text
        assert str(fake) not in text and str(source) not in text
        checks.append(
            "Two legacy installs detected; unregistered verified copy included"
        )

        def old_untouched():
            assert (old_a / "IndexTTSVoiceApp.exe").is_file()
            assert (old_b / "IndexTTSVoiceApp.exe").is_file()
            assert registry_version() == "0.4.3"

        assert run(installer, f"/DIR={new}") != 0
        old_untouched()
        checks.append("Silent upgrade without consent blocked before uninstall")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 7862))
            listener.listen()
            assert run(installer, "/UPGRADE=1", f"/DIR={new}") != 0
        old_untouched()
        checks.append("Busy backend port blocked before uninstall")
        # A private, harmless waiting process uses the frontend filename to
        # exercise the native process check. No microphone/GPU/service is used.
        running = root / "running-fixture"
        running.mkdir()
        running_exe = running / "IndexTTSVoiceApp.exe"
        shutil.copyfile(
            Path(os.environ["SystemRoot"]) / "System32/ping.exe", running_exe
        )
        process = subprocess.Popen(
            [str(running_exe), "-t", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            assert process.poll() is None
            assert run(installer, "/UPGRADE=1", f"/DIR={new}") != 0
            assert process.poll() is None  # Installer must not kill any process.
            old_untouched()
        finally:
            process.terminate()  # Only the specific process created by this test.
            process.wait(timeout=5)
        checks.append("Running frontend blocked before uninstall; not force-killed")
        for bad in (fake, source, old_b / "nested"):
            assert run(installer, "/UPGRADE=1", f"/DIR={bad}") != 0
            old_untouched()
        checks.append("Portable/source/nested target overwrite refused")

        assert run(installer, "/UPGRADE=1", f"/DIR={new}") == 0
        assert not (old_a / "IndexTTSVoiceApp.exe").exists()
        assert not (old_b / "IndexTTSVoiceApp.exe").exists()
        assert (new / "index_tts_voice_app-0.4.4-py3-none-any.whl").is_file()
        assert registry_version() == "0.4.4"
        for path, content in sentinels.items():
            assert path.read_bytes() == content
        assert (fake / "IndexTTSVoiceApp.exe").read_bytes() == b"portable fixture"
        checks.append("Both old programs removed via their own verified uninstallers")
        checks.append(
            "Versioned v0.4.4 installed; engine/presets/settings byte-identical"
        )

        assert "count=1" in plan(installer, "after.txt")
        assert run(installer, "/UPGRADE=1", f"/DIR={new}") == 0
        assert registry_version() == "0.4.4"
        checks.append("Same-version repair/reinstall successful")
        downgrade = compile_installer("0.4.3")
        assert run(downgrade, "/UPGRADE=1", f"/DIR={old_b}") != 0
        assert registry_version() == "0.4.4"
        assert (new / "IndexTTSVoiceApp.exe").exists()
        checks.append("Downgrade refused; current installation retained")

        # With no old installs remaining, a fresh installation needs no upgrade
        # flag and must not be blocked by the custom confirmation page.
        assert run(new / "unins000.exe") == 0
        deadline = time.monotonic() + 10
        while registry_version() is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert registry_version() is None
        assert "count=0" in plan(installer, "fresh.txt")
        assert run(installer, f"/DIR={fresh}") == 0
        assert registry_version() == "0.4.4"
        checks.append("Fresh install without upgrade consent successful")
    finally:
        for directory in installed:
            for uninstaller in directory.glob("unins*.exe"):
                # Only our exact generated TEST installation paths, NEVER real apps.
                assert directory.parent == root
                assert (
                    uninstaller.with_suffix(".dat").read_bytes()[64:102]
                    == TEST_ID.encode()
                )
                assert run(uninstaller) == 0
        deadline = time.monotonic() + 10
        while registry_version() is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert registry_version() is None
        (root / "results.json").write_text(
            json.dumps(
                {
                    "test_root": str(root),
                    "checks": checks,
                    "test_registry_cleaned": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Installer upgrade evidence: {root / 'results.json'}")
