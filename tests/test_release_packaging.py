import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from bundle_rules import filter_binaries, include_binary, include_data
from prepare_release import copy_archive_notices, fetch

from voice_app.third_party import create_notice_dialog, verify_notices


@pytest.mark.parametrize(
    "path",
    [
        "PySide6/Qt6VirtualKeyboard.dll",
        "PySide6/Qt6Pdf.dll",
        "PySide6/Qt6Qml.dll",
        "PySide6/QtVirtualKeyboard.pyd",
        "PySide6/Qt6Quick.dll",
        "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
        "PySide6/plugins/imageformats/qpdf.dll",
        "PySide6/plugins/imageformats/unknown-plugin.dll",
        "PySide6/qml/QtQuick/VirtualKeyboard/plugin.dll",
        "icuuc.dll",
    ],
)
def test_unreviewed_qt_modules_and_plugins_are_not_bundled(path):
    assert not include_binary(path)


@pytest.mark.parametrize(
    "path",
    [
        r"PySide6\Qt6Core.dll",
        "PySide6/Qt6Gui.dll",
        "PySide6/Qt6Widgets.dll",
        "PySide6/plugins/platforms/qwindows.dll",
        "PySide6/plugins/platforms/qoffscreen.dll",
        "PySide6/plugins/imageformats/qjpeg.dll",
        "python311.dll",
        "libssl-3-x64.dll",
    ],
)
def test_required_frontend_files_are_retained(path):
    assert include_binary(path)


def test_binary_filter_preserves_original_entries():
    good = ("PySide6/Qt6Core.dll", "fixture/Qt6Core.dll", "BINARY")
    bad = ("PySide6/Qt6VirtualKeyboard.dll", "fixture/Qt6VirtualKeyboard.dll", "BINARY")
    assert filter_binaries([good, bad]) == [good]


def test_unneeded_qt_data_is_not_bundled():
    assert not include_data("PySide6/translations/qtbase_zh_CN.qm")
    assert not include_data("PySide6/qml/QtQuick/VirtualKeyboard/Main.qml")
    assert include_data("certifi/cacert.pem")


def make_archive(path, members):
    with tarfile.open(path, "w:xz") as archive:
        for name, data, kind in members:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                member.linkname = "../../outside"
                archive.addfile(member)


def test_archive_notices_are_copied_without_executing_or_following_links(tmp_path):
    source = tmp_path / "source.tar.xz"
    make_archive(
        source,
        [
            ("source/LICENSES/LGPL-3.0-only.txt", b"fixture license", tarfile.REGTYPE),
            ("source/tools/build.py", b"not to be executed", tarfile.REGTYPE),
            ("source/LICENSE-link", b"", tarfile.SYMTYPE),
        ],
    )
    destination = tmp_path / "notices"
    copied = copy_archive_notices(source, destination)
    assert copied == ["LICENSES/LGPL-3.0-only.txt"]
    assert (destination / copied[0]).read_bytes() == b"fixture license"
    assert not (destination / "tools").exists()
    assert not (tmp_path / "outside").exists()


def test_unsafe_source_archive_path_is_rejected(tmp_path):
    source = tmp_path / "unsafe.tar.xz"
    make_archive(source, [("../LICENSE", b"fixture", tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="Unsafe"):
        copy_archive_notices(source, tmp_path / "notices")


def test_verified_cache_is_reused_without_network(tmp_path):
    content = b"verified source"
    item = {
        "name": "source.tar.xz",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    (tmp_path / item["name"]).write_bytes(content)
    assert fetch(item, tmp_path).read_bytes() == content


def test_download_rejects_wrong_hash_and_preserves_original(tmp_path, monkeypatch):
    import prepare_release

    (tmp_path / "source.tar.xz").write_bytes(b"existing wrong cache preserved")
    expected = b"expected content"
    item = {
        "name": "source.tar.xz",
        "size": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
        "url": "https://example.invalid/source",
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"unverified"

    monkeypatch.setattr(prepare_release.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(prepare_release.time, "sleep", lambda *a: None)
    with pytest.raises(RuntimeError, match="mismatch"):
        fetch(item, tmp_path)
    assert (
        tmp_path / "source.tar.xz"
    ).read_bytes() == b"existing wrong cache preserved"


def minimal_notices(root):
    folder = root / "third-party"
    license_file = folder / "licenses" / "qtbase" / "LICENSES" / "LGPL-3.0-only.txt"
    license_file.parent.mkdir(parents=True)
    license_file.write_text("fixture license", encoding="utf-8")
    inventory = folder / "inventory.json"
    inventory.write_text(
        json.dumps({"artifacts": [{"name": "fixture-source"}]}), encoding="utf-8"
    )
    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (license_file, inventory)
    ]
    (folder / "files.sha256.json").write_text(json.dumps(records), encoding="utf-8")
    return license_file


def test_notice_verification_detects_missing_or_modified_files(tmp_path):
    path = minimal_notices(tmp_path)
    assert verify_notices(tmp_path)["passed"]
    path.write_text("modified", encoding="utf-8")
    assert not verify_notices(tmp_path)["passed"]
    path.unlink()
    assert not verify_notices(tmp_path)["passed"]


def test_notice_inventory_cannot_escape_bundle(tmp_path):
    minimal_notices(tmp_path)
    inventory = tmp_path / "third-party" / "files.sha256.json"
    inventory.write_text(
        json.dumps([{"path": "../private.txt", "sha256": "0" * 64}]), encoding="utf-8"
    )
    result = verify_notices(tmp_path)
    assert not result["passed"] and "Unsafe inventory path" in result["problems"]


def test_notice_viewer_is_readable_and_contains_source_folder_action(tmp_path):
    from PySide6.QtWidgets import QApplication, QPushButton, QTextBrowser

    app = QApplication.instance() or QApplication([])
    (tmp_path / "THIRD_PARTY.md").write_text("Visible attribution", encoding="utf-8")
    minimal_notices(tmp_path)
    dialog = create_notice_dialog(root=tmp_path)
    dialog.show()
    app.processEvents()
    assert (
        "Visible attribution"
        in dialog.findChild(QTextBrowser, "licenseText").toPlainText()
    )
    assert any(
        "源码" in button.text() and button.isEnabled()
        for button in dialog.findChildren(QPushButton)
    )
    dialog.close()
    app.processEvents()


def test_setup_and_main_window_expose_notice_entry(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QPushButton

    from voice_app import client_main, desktop_qt

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(client_main, "cable_is_installed", lambda: True)
    setup = client_main.SetupDialog(tmp_path)
    assert setup.findChild(QPushButton, "thirdPartyLicenses") is not None
    setup.close()
    monkeypatch.setattr(desktop_qt.VoiceDesktopQt, "_refresh_data", lambda self: None)
    window = desktop_qt.VoiceDesktopQt("http://127.0.0.1:1")
    assert window.third_party_button.text() == "第三方许可"
    window.close()
    app.processEvents()
