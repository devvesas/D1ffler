"""Refresh reviewed notices and source archives after an explicit dependency update.

Downloads source data only; never executes upstream code. Run from the exact
locked environment and review the resulting inventory before committing it.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as metadata
import json
import posixpath
import platform
from pathlib import Path, PurePosixPath
import sys
import tarfile
import urllib.request
import zipfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_app import locked_versions

LEGAL_NAMES = ("license", "copying", "copyright", "notice", "patents")


def download(url, destination):
    if destination.is_file():
        return destination.read_bytes()
    print(f"Downloading {destination.name}...", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read(200 * 1024 * 1024 + 1)
    if len(content) > 200 * 1024 * 1024:
        raise ValueError("Source download exceeds the supported size")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return content


def copy_package_notices(root, name, distribution):
    """Keep vendored metadata paths so identically named notices cannot collide."""
    files = []
    for item in distribution.files or []:
        path = PurePosixPath(str(item))
        if not path.name.lower().startswith(LEGAL_NAMES) or not any(part.endswith(".dist-info") for part in path.parts):
            continue
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe package notice path: {path}")
        parts = path.parts
        if parts[0].endswith(".dist-info"):
            parts = parts[1:]
            if parts[0] == "licenses":
                parts = parts[1:]
        destination = root / "licenses" / "packages" / name / Path(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(distribution.locate_file(item).read_bytes())
        files.append(destination.relative_to(root).as_posix())
    if not files:
        raise ValueError(f"No upstream license text found for {name}")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "licensing" / "inventory.json")
    args = parser.parse_args()
    if platform.python_version() != "3.10.7" or sys.platform != "win32":
        raise ValueError("This source/license profile requires Windows Python 3.10.7; review its sources and notices when updating the interpreter")
    cache = ROOT / "build" / "license-sources"
    cache.mkdir(parents=True, exist_ok=True)
    inventory = {"schema_version": 1, "application_license": "GPL-3.0-or-later", "python_version": "3.10.7", "packages": {}, "sources": []}
    expected = locked_versions(ROOT / "requirements" / "test.lock.txt")
    for name, version in expected.items():
        distribution = metadata.distribution(name)
        if distribution.version != version:
            raise ValueError(f"Install the test lock first: {name} is {distribution.version}, expected {version}")
        files = copy_package_notices(ROOT, name, distribution)
        inventory["packages"][name] = {"version": version, "license_files": files, "origin": f"https://pypi.org/project/{name}/{version}/"}
    # The Windows Python distribution includes the core and bundled-library notices.
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    destination = ROOT / "licenses" / "Python-3.10.7.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(python_license.read_bytes())

    # Preserve Microsoft's official document and an offline text rendering.
    runtime_url = "https://visualstudio.microsoft.com/wp-content/uploads/2021/09/Visual-C-Runtime-2015-2022-License-1.docx"
    runtime_document = ROOT / "licenses" / "Microsoft-VC-Runtime-2015-2022.docx"
    download(runtime_url, runtime_document)
    with zipfile.ZipFile(runtime_document) as document:
        tree = ElementTree.fromstring(document.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = [
        "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        for paragraph in tree.findall(".//w:p", namespace)]
    runtime_document.with_suffix(".txt").write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    inventory["microsoft_runtime_terms"] = {"origin": runtime_url, "document": runtime_document.relative_to(ROOT).as_posix()}

    sources = []
    for name in ("PyQt6", "PyQt6_sip", "regex", "pyinstaller"):
        version = expected[name]
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as response:
            release = json.load(response)
        sdist = next(item for item in release["urls"] if item["packagetype"] == "sdist")
        sources.append({"component": name, "version": version, "filename": sdist["filename"], "url": sdist["url"], "sha256": sdist["digests"]["sha256"]})
    qt = expected["PyQt6-Qt6"]
    for module in ("qtbase", "qtsvg", "qtimageformats"):
        filename = f"{module}-everywhere-src-{qt}.tar.xz"
        url = f"https://download.qt.io/archive/qt/{'.'.join(qt.split('.')[:2])}/{qt}/submodules/{filename}"
        with urllib.request.urlopen(url + ".sha256", timeout=60) as response:
            checksum = response.read().decode("ascii").split()[0]
        sources.append({"component": module, "version": qt, "filename": filename, "url": url, "sha256": checksum})
    for component, version, url in (
        ("CPython", "3.10.7", "https://www.python.org/ftp/python/3.10.7/Python-3.10.7.tar.xz"),
        ("libffi", "3.3.0", "https://github.com/python/cpython-source-deps/archive/refs/tags/libffi-3.3.0.tar.gz"),
        ("bzip2", "1.0.8", "https://github.com/python/cpython-source-deps/archive/refs/tags/bzip2-1.0.8.tar.gz"),
        ("xz", "5.2.5", "https://github.com/python/cpython-source-deps/archive/refs/tags/xz-5.2.5.tar.gz"),
        ("zlib", "1.2.12", "https://github.com/python/cpython-source-deps/archive/refs/tags/zlib-1.2.12.tar.gz"),
    ):
        filename = "Python-3.10.7.tar.xz" if component == "CPython" else f"{component}-{version}.tar.gz"
        content = download(url, cache / filename)
        sources.append({"component": component, "version": version, "filename": filename, "url": url, "sha256": hashlib.sha256(content).hexdigest()})
    for source in sources:
        path = cache / source["filename"]
        content = download(source["url"], path)
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise ValueError(f"Upstream source checksum mismatch: {path.name}")
        # Preserve license/attribution texts and their relative upstream paths.
        # Read member streams directly; never extract arbitrary tar paths to disk.
        if source["component"] in {"qtbase", "qtsvg", "qtimageformats", "CPython", "libffi", "bzip2", "xz", "zlib"}:
            with tarfile.open(path) as archive:
                referenced = set()
                for member in archive.getmembers():
                    if member.isfile() and member.name.endswith("qt_attribution.json"):
                        records = json.load(archive.extractfile(member), strict=False)
                        records = records if isinstance(records, list) else [records]
                        for record in records:
                            for filename in str(record.get("LicenseFile", "")).split():
                                referenced.add(posixpath.normpath(posixpath.join(posixpath.dirname(member.name), filename)))
                for member in archive.getmembers():
                    relative = PurePosixPath(member.name)
                    if not member.isfile() or relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2:
                        continue
                    zlib_notice = source["component"] == "zlib" and relative.name in {"README", "zlib.h"}
                    python_crt_notice = source["component"] == "CPython" and relative.name == "crtlicense.txt"
                    if not (relative.name.lower().startswith(LEGAL_NAMES) or relative.name == "qt_attribution.json" or "LICENSES" in relative.parts or member.name in referenced or zlib_notice or python_crt_notice):
                        continue
                    destination = ROOT / "licenses" / f"{source['component']}-{source['version']}" / Path(*relative.parts[1:])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.extractfile(member).read())
        inventory["sources"].append(source)
    inventory["notice_files"] = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT / "licenses").rglob("*")) if path.is_file()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Review and commit {args.output}; source archives remain in {cache}")


if __name__ == "__main__":
    main()
