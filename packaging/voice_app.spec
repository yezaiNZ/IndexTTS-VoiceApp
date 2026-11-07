# PyInstaller specification: reviewed plugin allowlist is applied before COLLECT.
import sys
from pathlib import Path

project_root = Path(SPECPATH).parent
sys.path.insert(0, str(project_root / "packaging"))
from bundle_rules import filter_binaries, include_data

a = Analysis(
    [str(project_root / "voice_app" / "launcher_entry.py")],
    pathex=[str(project_root)],
    binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
a.binaries = filter_binaries(a.binaries)
a.datas = [item for item in a.datas if include_data(item[0])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="IndexTTSVoiceApp",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="IndexTTSVoiceApp")
