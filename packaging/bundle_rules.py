"""Keep this Widgets-only application free of unrelated Qt plugins/modules."""

from pathlib import PurePosixPath

QT_LIBRARIES = frozenset(
    {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll", "qt6network.dll", "qt6opengl.dll"}
)
QT_PLUGINS = frozenset(
    {
        "platforms/qwindows.dll",
        "platforms/qoffscreen.dll",
        "platforms/qminimal.dll",
        "styles/qmodernwindowsstyle.dll",
        "imageformats/qgif.dll",
        "imageformats/qico.dll",
        "imageformats/qjpeg.dll",
    }
)


def include_binary(destination: str) -> bool:
    path = destination.replace("\\", "/").lower()
    name = PurePosixPath(path).name
    if name.startswith("qt6") and name.endswith(".dll"):
        return name in QT_LIBRARIES
    if "pyside6/" in path and name.startswith("qt") and name.endswith(".pyd"):
        return name in {"qtcore.pyd", "qtgui.pyd", "qtwidgets.pyd", "qtnetwork.pyd"}
    if "pyside6/plugins/" in path:
        plugin = path.split("pyside6/plugins/", 1)[1]
        return plugin in QT_PLUGINS
    if "pyside6/qml/" in path:
        return False
    return name not in {"icuuc.dll", "icudt78.dll"}


def include_data(destination: str) -> bool:
    path = destination.replace("\\", "/").lower()
    return not any(part in path for part in ("pyside6/qml/", "pyside6/translations/"))


def filter_binaries(entries):
    return [entry for entry in entries if include_binary(entry[0])]
