"""Collect actual frontend notices and verified corresponding Qt sources.

Never installs a driver, touches the inference environment, or starts the model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath

import requests
from bundle_rules import include_binary, include_data

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PROJECT_ROOT / "packaging" / "third-party-sources.json"
PACKAGES = (
    "requests",
    "urllib3",
    "certifi",
    "charset-normalizer",
    "idna",
    "pyinstaller",
    "altgraph",
    "packaging",
    "pefile",
    "pyinstaller-hooks-contrib",
    "pywin32-ctypes",
    "setuptools",
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified(path: Path, item: dict) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == item["size"]
        and sha256(path) == item["sha256"]
    )


def fetch(item: dict, cache: Path) -> Path:
    name = item["name"]
    if PurePosixPath(name).name != name or "\\" in name:
        raise ValueError("Manifest filenames must be plain basenames")
    destination = cache / name
    if verified(destination, item):
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    urls = [item["url"], *item.get("alternate_urls", [])]
    for attempt in range(3):
        try:
            print(f"Fetching verified notice/source: {name}", flush=True)
            with requests.get(
                urls[attempt % len(urls)], stream=True, timeout=(20, 45)
            ) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if not verified(partial, item):
                raise RuntimeError(f"Size/SHA-256 mismatch: {name}")
            partial.replace(destination)
            return destination
        except (requests.RequestException, OSError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise AssertionError("Unreachable")


def copy_archive_notices(archive: Path, destination: Path) -> list[str]:
    """Read regular license/attribution files only; never extract archive paths."""
    copied = []
    with tarfile.open(archive, "r:xz") as source:
        for member in source:
            path = PurePosixPath(member.name)
            if not member.isfile() or member.size > 2 * 1024 * 1024:
                continue
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or any(":" in part for part in path.parts)
            ):
                raise ValueError("Unsafe source archive path")
            name = path.name.lower()
            if not (
                "LICENSES" in path.parts
                or name.startswith(
                    ("license", "licence", "copying", "notice", "copyright")
                )
                or name == "qt_attribution.json"
                or name.endswith(".qt_attribution.json")
            ):
                continue
            relative = Path(*path.parts[1:])
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is not None:
                with stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                copied.append(relative.as_posix())
    if not copied:
        raise RuntimeError(f"Source archive contains no notices: {archive.name}")
    return copied


def copy_distribution_notices(name: str, root: Path) -> dict:
    distribution = metadata.distribution(name)
    copied = []
    for entry in distribution.files or ():
        relative = PurePosixPath(str(entry).replace("\\", "/"))
        basename = relative.name.lower()
        if not basename.startswith(
            ("license", "licence", "copying", "notice", "authors")
        ):
            continue
        source = Path(distribution.locate_file(entry))
        if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
            continue
        # Preserve distinct nested notices without leaking build-machine paths.
        safe = [part for part in relative.parts if part not in {"..", "."}]
        target = root / name / Path(*safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target.relative_to(root).as_posix())
    if not copied:
        raise RuntimeError(f"No installed notice files found for {name}")
    return {"name": name, "version": distribution.version, "notices": copied}


def prepare(bundle: Path, cache: Path) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    qt_version = manifest["qt_version"]
    if sys.version.split()[0] != manifest["python_version"]:
        raise RuntimeError("Build Python differs from the reviewed license inventory")
    if ssl.OPENSSL_VERSION.split()[1] != manifest["openssl_version"]:
        raise RuntimeError("Build OpenSSL differs from the reviewed license inventory")
    for name in ("PySide6-Essentials", "shiboken6"):
        if metadata.version(name) != qt_version:
            raise RuntimeError(f"{name} version does not match reviewed Qt sources")
    uv = bundle / "uv.exe"
    if not uv.is_file():
        raise RuntimeError("Bundle must contain the actual uv executable")
    uv_output = subprocess.check_output([str(uv), "--version"], text=True).strip()
    if uv_output.split()[1] != manifest["uv_version"]:
        raise RuntimeError("Bundled uv version does not match notice pins")
    root = bundle / "third-party"
    root.mkdir(parents=True, exist_ok=True)
    sources = []
    for item in manifest["artifacts"]:
        source = fetch(item, cache)
        if item["source_archive"]:
            destination = root / "sources" / item["name"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copy_archive_notices(source, root / "licenses" / item["component"])
        else:
            destination = root / "licenses" / item["component"] / item["name"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        sources.append({**item, "path": destination.relative_to(bundle).as_posix()})
    packages = [copy_distribution_notices(name, root / "licenses") for name in PACKAGES]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("The build Python's license file is missing")
    target = root / "licenses" / "python" / "LICENSE.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, target)
    engine_archive = next((bundle / "engine-source").glob("index-tts-*.zip"))
    with zipfile.ZipFile(engine_archive) as archive:
        names = [
            n
            for n in archive.namelist()
            if PurePosixPath(n).name in {"LICENSE", "LICENSE_ZH.txt"}
        ]
        if len(names) < 2:
            raise RuntimeError("The pinned IndexTTS archive lacks upstream licenses")
        for name in names:
            target = root / "licenses" / "indextts" / PurePosixPath(name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    instructions = PROJECT_ROOT / "packaging" / "THIRD_PARTY_SOURCES.md"
    shutil.copyfile(instructions, root / "README.md")
    inventory = {
        "schema": 1,
        "qt_version": qt_version,
        "python_version": sys.version.split()[0],
        "openssl_version": ssl.OPENSSL_VERSION.split()[1],
        "uv_version": manifest["uv_version"],
        "qt_modified": False,
        "pyside_shiboken_modified": False,
        "qt_linkage": "shared DLLs; replaceable in the onedir distribution",
        "packages": packages,
        "artifacts": sources,
    }
    (root / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "files.sha256.json":
            records.append(
                {"path": path.relative_to(bundle).as_posix(), "sha256": sha256(path)}
            )
    (root / "files.sha256.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return inventory


def audit(bundle: Path) -> dict:
    problems = []
    root = bundle / "third-party"
    try:
        records = json.loads((root / "files.sha256.json").read_text(encoding="utf-8"))
        for record in records:
            relative = PurePosixPath(record["path"])
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.parts[0] != "third-party"
            ):
                problems.append("Unsafe notice inventory path")
                continue
            path = bundle / Path(*relative.parts)
            if not path.is_file() or sha256(path) != record["sha256"]:
                problems.append(f"Missing/modified notice: {record['path']}")
        inventory = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
        pinned = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for item in pinned["artifacts"]:
            matches = [i for i in inventory["artifacts"] if i["name"] == item["name"]]
            if len(matches) != 1 or not verified(bundle / matches[0]["path"], item):
                problems.append(f"Missing/invalid pinned artifact: {item['name']}")
        for name in PACKAGES:
            if not any((root / "licenses" / name).rglob("*")):
                problems.append(f"Missing package notices: {name}")
        if not list((root / "licenses" / "qtbase").rglob("LGPL-3.0-only.txt")):
            problems.append("Missing LGPLv3 license text")
        for relative in (
            "README.md",
            "licenses/python/LICENSE.txt",
            "licenses/indextts/LICENSE",
            "licenses/indextts/LICENSE_ZH.txt",
        ):
            if not (root / relative).is_file():
                problems.append(f"Missing notice: {relative}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"Cannot read notice inventory: {exc}")
        inventory = {}
    for path in (bundle / "_internal").rglob("*"):
        if path.is_file():
            relative = path.relative_to(bundle / "_internal").as_posix()
            if not include_binary(relative) or not include_data(relative):
                problems.append(f"Unexpected Qt binary/plugin/data: {relative}")
    for relative in (
        "PySide6/Qt6Core.dll",
        "PySide6/Qt6Gui.dll",
        "PySide6/Qt6Widgets.dll",
        "PySide6/plugins/platforms/qwindows.dll",
    ):
        if not (bundle / "_internal" / relative).is_file():
            problems.append(f"Required Qt runtime file missing: {relative}")
    result = {
        "passed": not problems,
        "problems": problems,
        "qt_version": inventory.get("qt_version"),
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--cache", type=Path, default=PROJECT_ROOT / "build" / "third-party-cache"
    )
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if not args.audit_only:
        prepare(args.bundle.resolve(), args.cache.resolve())
    result = audit(args.bundle.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
