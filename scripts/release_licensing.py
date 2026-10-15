"""Validate reviewed license inputs and assemble exact corresponding source."""
from __future__ import annotations
import ast
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile

DOCUMENTS = ("LICENSE", "COPYRIGHT.md", "THIRD_PARTY_NOTICES.md", "SOURCE_ACCESS.md")
QT_LIBRARIES = {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll", "qt6svg.dll"}
EXCLUDED_BINARIES = {"qt6pdf.dll", "qt6network.dll", "qpdf.dll", "qtuiotouchplugin.dll", "opengl32sw.dll"}
EXCLUDED_MODULES = ["ssl", "_ssl", "_hashlib", "PyQt6.QtPdf", "PyQt6.QtNetwork"]
HASH_MODULES = ["_sha1", "_sha256", "_sha512", "_md5", "_sha3", "_blake2"]
REVIEWED_NATIVE = QT_LIBRARIES | {
    "python310.dll", "python3.dll", "libffi-7.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "qtcore.pyd", "qtgui.pyd", "qtwidgets.pyd", "qtsvg.pyd",
    "sip.cp310-win_amd64.pyd", "_regex.cp310-win_amd64.pyd", "select.pyd", "_lzma.pyd", "_bz2.pyd",
    "pyexpat.pyd", "unicodedata.pyd", "_decimal.pyd", "_multiprocessing.pyd", "_socket.pyd", "_ctypes.pyd", "_queue.pyd", "_uuid.pyd",
    "_sha1.pyd", "_sha256.pyd", "_sha512.pyd", "_md5.pyd", "_sha3.pyd", "_blake2.pyd",
    "qmodernwindowsstyle.dll", "qsvg.dll", "qtiff.dll", "qwbmp.dll", "qwindows.dll", "qsvgicon.dll",
    "qoffscreen.dll", "qico.dll", "qjpeg.dll", "qtga.dll", "qgif.dll", "qicns.dll", "qminimal.dll", "qwebp.dll",
}


def native_version(path):
    """Numeric Windows version metadata for shipped DLLs, where present."""
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer) or not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
    return f"{values[4] >> 16}.{values[4] & 65535}.{values[5] >> 16}.{values[5] & 65535}"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate(root, locked, python_version):
    for name in DOCUMENTS:
        if not (root / name).is_file() or not (root / name).stat().st_size:
            raise ValueError(f"Required licensing document is missing: {name}")
    inventory = json.loads((root / "licensing" / "inventory.json").read_text(encoding="utf-8"))
    if inventory.get("application_license") != "GPL-3.0-or-later":
        raise ValueError("Review the licensing inventory for the selected application license")
    if inventory["python_version"] != python_version:
        raise ValueError("Python version changed; refresh its license/source inventory before releasing")
    versions = {name: value["version"] for name, value in inventory["packages"].items()}
    if versions != locked:
        raise ValueError("Dependency lock changed; refresh and review the licensing inventory")
    actual_notices = {path.relative_to(root).as_posix(): digest(path) for path in (root / "licenses").rglob("*") if path.is_file()}
    if not actual_notices or actual_notices != inventory["notice_files"]:
        raise ValueError("License texts changed or are missing; refresh the licensing inventory")
    for package in inventory["packages"].values():
        if not package.get("license_files") or any(name not in actual_notices for name in package["license_files"]):
            raise ValueError("Dependency license text is missing from the inventory")
    required_sources = {"PyQt6", "PyQt6_sip", "regex", "pyinstaller", "qtbase", "qtsvg", "qtimageformats", "CPython", "libffi", "bzip2", "xz", "zlib"} if "PyQt6" in locked else set()
    if not inventory["sources"] or required_sources - {source["component"] for source in inventory["sources"]}:
        raise ValueError("Required corresponding-source components are missing from the inventory")
    for source in inventory["sources"]:
        filename = Path(source["filename"])
        if filename.name != source["filename"] or not source["url"].startswith("https://") or len(source["sha256"]) != 64:
            raise ValueError("Invalid corresponding-source entry")
    return inventory


def ensure_sources(root, inventory):
    cache = root / "build" / "license-sources"
    cache.mkdir(parents=True, exist_ok=True)
    files = []
    for source in inventory["sources"]:
        path = cache / source["filename"]
        if not path.is_file():
            print(f"Downloading corresponding source: {path.name}...", flush=True)
            temporary = path.with_suffix(path.suffix + ".partial")
            try:
                with urllib.request.urlopen(source["url"], timeout=60) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                if digest(temporary) != source["sha256"]:
                    raise ValueError(f"Corresponding-source checksum mismatch: {path.name}")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        if digest(path) != source["sha256"]:
            raise ValueError(f"Corresponding-source checksum mismatch: {path.name}")
        files.append(path)
    return files


def freeze_spec(root, entry, executable, resource, gui):
    data = [(str(root / name), name) for name in ("icons", "symbols", "logo", "licenses", "licensing")]
    data += [(str(root / name), ".") for name in DOCUMENTS]
    return (f"a = Analysis([{str(root / entry)!r}], pathex=[{str(root / 'src')!r}, {str(root)!r}], binaries=[], datas={data!r}, "
            f"hiddenimports={HASH_MODULES!r}, hookspath=[], hooksconfig={{}}, runtime_hooks=[], excludes={EXCLUDED_MODULES!r}, noarchive=False)\n"
            f"excluded = {sorted(EXCLUDED_BINARIES)!r}\n"
            "a.binaries = [item for item in a.binaries if item[0].replace('\\\\', '/').rsplit('/', 1)[-1].lower() not in excluded]\n"
            "pyz = PYZ(a.pure)\n"
            f"exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name={Path(executable).stem!r}, "
            f"debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console={not gui!r}, "
            f"version={str(resource)!r})\n")


def audit_binaries(work, executable):
    # Inspect the final EXE input, after filtering, rather than Analysis's superset.
    path = work / Path(executable).stem / Path(executable).stem / "PKG-00.toc"
    data = ast.literal_eval(path.read_text(encoding="utf-8"))
    def records(value):
        if isinstance(value, (tuple, list)):
            if len(value) == 3 and isinstance(value[0], str) and isinstance(value[-1], str) and value[-1] in {"BINARY", "EXTENSION"}:
                yield value
            else:
                for child in value:
                    yield from records(child)
    inputs = {item[0].replace("\\", "/"): Path(item[1]) for item in records(data)}
    binaries = sorted(inputs)
    basenames = {Path(name).name.lower() for name in binaries}
    qt = {name for name in basenames if name.startswith("qt6") and name.endswith(".dll")}
    if qt != QT_LIBRARIES:
        raise ValueError(f"Frozen Qt modules changed; review licensing/source inventory: {sorted(qt)}")
    forbidden = EXCLUDED_BINARIES | {"libssl-1_1.dll", "libcrypto-1_1.dll", "_ssl.pyd", "_hashlib.pyd"}
    if basenames & forbidden or any(name.startswith(("libcrypto", "libssl")) for name in basenames):
        raise ValueError("Unreviewed SSL/PDF/software-OpenGL dependency entered the frozen app")
    if basenames - REVIEWED_NATIVE:
        raise ValueError(f"Unreviewed native component entered the frozen app: {sorted(basenames - REVIEWED_NATIVE)}")
    return [{"path": name, "sha256": digest(inputs[name]) if inputs[name].is_file() else None,
             "version": native_version(inputs[name]) if inputs[name].is_file() else None} for name in binaries]


def assemble(root, candidate, package, record, inventory, sources):
    for name in DOCUMENTS:
        shutil.copy2(root / name, package / name)
    for name in ("licenses", "licensing"):
        shutil.copytree(root / name, package / name)
    archive = candidate / f"D1ffler-{record['version']}-corresponding-source.zip"
    temporary = archive.with_suffix(".zip.partial")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, expected in record["source"]["files_sha256"].items():
            path = root / name
            if digest(path) != expected:
                raise ValueError(f"Source changed during the build: {name}")
            bundle.write(path, f"D1ffler-source/{name}")
        for source in sources:
            bundle.write(source, f"third-party-source/{source.name}")
        bundle.write(root / "SOURCE_ACCESS.md", "SOURCE_ACCESS.md")
    with zipfile.ZipFile(temporary) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Corresponding-source archive integrity check failed")
    temporary.rename(archive)
    info = {"status": "verified_materials", "application_license": "GPL-3.0-or-later",
            "inventory_sha256": digest(root / "licensing" / "inventory.json"),
            "source_archive": {"filename": archive.name, "sha256": digest(archive), "bytes": archive.stat().st_size},
            "sources": inventory["sources"]}
    # This notice travels inside the binary ZIP and identifies its required companion.
    (package / "SOURCE_ARCHIVE.txt").write_text(
        f"Corresponding source: {archive.name}\nSHA-256: {info['source_archive']['sha256']}\n"
        "Publish this source archive beside the binary ZIP, with equivalent free access.\n"
        "See SOURCE_ACCESS.md for rebuilding, replacing Qt, and redistribution.\n", encoding="utf-8")
    return info
