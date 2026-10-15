"""Build and verify local Windows executables; signing is the user's responsibility."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from d1ffler.app_version import APP_VERSION
from scripts import release_licensing


def run(command, log, *, cwd, environment=None):
    """Every native exit status is checked, including pip, pytest, and freezing."""
    print(f"Running {log.stem}...", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run([str(item) for item in command], cwd=cwd,
                                   env=environment, stdout=stream, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError(f"{log.stem} failed ({completed.returncode}); see {log}")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def locked_versions(path):
    versions = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("-r "):
            versions.update(locked_versions(path.parent / line[3:]))
        elif line and not line.startswith("#"):
            name, version = line.split("==")
            versions[name] = version
    return versions


def version_resource(executable):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z0-9.]+))?", APP_VERSION)
    if not match:
        raise ValueError("APP_VERSION must use major.minor.patch[-prerelease]")
    numbers = tuple(map(int, match.group(1, 2, 3))) + (0,)
    if any(number > 65535 for number in numbers):
        raise ValueError("Version components exceed Windows resource limits")
    strings = {"FileDescription": "D1ffler desktop comparison" if executable == "D1ffler.exe" else "D1ffler command-line comparison",
               "FileVersion": APP_VERSION, "ProductVersion": APP_VERSION,
               "ProductName": "D1ffler", "OriginalFilename": executable}
    entries = ",".join(f"StringStruct({key!r}, {value!r})" for key, value in strings.items())
    return (f"VSVersionInfo(ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, "
            f"mask=0x3f, flags={2 if match[4] else 0}, OS=0x40004, fileType=1, subtype=0, date=(0,0)), "
            f"kids=[StringFileInfo([StringTable('040904b0', [{entries}])]), "
            "VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n")


def is_link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def source_record(root):
    root = Path(root).resolve()
    # Include uncommitted and untracked release inputs; a revision alone is insufficient.
    try:
        checkout = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                  capture_output=True, text=True, check=True).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        checkout = None
    if checkout is None or Path(checkout).resolve() != root.resolve():
        inventory = {}
        for directory, subdirectories, filenames in os.walk(root, followlinks=False):
            subdirectories[:] = [name for name in subdirectories
                                 if not name.startswith(".") and name not in {"build", "dist", "venv", "env", "__pycache__"}
                                 and not is_link(Path(directory) / name)]
            for name in filenames:
                path = Path(directory) / name
                if is_link(path) or path.suffix.lower() in {".pyc", ".pyo", ".log", ".tmp", ".spec"}:
                    continue
                inventory[path.relative_to(root).as_posix()] = digest(path)
        return {"revision": None, "dirty": True, "status": ["Source snapshot without Git metadata"],
                "files_sha256": dict(sorted(inventory.items()))}
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True).stdout
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    inventory = {name: digest(root / name) for name in sorted(set(listed)) if name and (root / name).is_file()}
    return {"revision": revision, "dirty": bool(status), "status": status.splitlines(), "files_sha256": inventory}


def test_results(path, require_success=True):
    cases = list(ET.parse(path).getroot().iter("testcase"))
    skipped = [{"test": f"{case.get('classname')}.{case.get('name')}", "reason": case.find("skipped").get("message", "")}
               for case in cases if case.find("skipped") is not None]
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    if require_success and (not cases or failed):
        raise RuntimeError("Source-test report is empty or contains failures")
    return {"status": "failed" if not cases or failed else "passed", "cases": len(cases), "failed": failed,
            "skipped": skipped, "junit_file": path.name}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build(args, root=ROOT, runner=run):
    if os.name != "nt" or struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Release builds require Windows x64 and 64-bit Python")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10+ is required")
    if args.release and (args.skip_install or args.skip_smoke_tests):
        raise ValueError("Release verification requires a fresh environment and packaged checks")
    version_resource("D1ffler.exe")
    name = f"D1ffler-{APP_VERSION}-windows-x64"
    build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    candidate = root / "dist" / "candidates" / f"{name}-{build_id}"
    candidate.mkdir(parents=True, exist_ok=False)
    logs = candidate / "verification"
    logs.mkdir()
    work = root / "build" / build_id
    work.mkdir(parents=True, exist_ok=False)
    record = {"schema_version": 1, "application": "D1ffler", "version": APP_VERSION,
              "build_id": build_id, "built_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "building", "ready_for_publication": False,
              "target": "Windows 10 1809+ / Windows 11, x64",
              "host": {"os": platform.platform(), "python": platform.python_version()},
              "source_tests": {"status": "skipped", "reason": "Use build.ps1 -Release"},
              "packaged_tests": {"status": "skipped", "reason": "Requested -SkipSmokeTests"},
              "clean_machine": {"status": "pending", "reason": "User responsibility; see README.md for build instructions"}}
    write_json(candidate / "release.json", record)
    try:
        record["source"] = source_record(root)
        record["package_kind"] = "user-build"
        inventory = release_licensing.validate(root, locked_versions(root / "requirements" / "test.lock.txt"), platform.python_version())
        dependency_sources = release_licensing.ensure_sources(root, inventory)
        lock = root / "requirements" / ("test.lock.txt" if args.release else "build.lock.txt")
        expected = locked_versions(lock)
        python = Path(sys.executable)
        if not args.skip_install:
            runner([python, "-m", "venv", work / "venv"], logs / "environment.log", cwd=root)
            python = work / "venv" / "Scripts" / "python.exe"
            runner([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", lock], logs / "install.log", cwd=root)
        record["isolated_environment"] = not args.skip_install
        code = "import importlib.metadata as m,json; print(json.dumps({n:m.version(n) for n in " + repr(list(expected)) + "}))"
        runner([python, "-c", code], logs / "dependencies.json", cwd=root)
        actual = json.loads((logs / "dependencies.json").read_text(encoding="utf-8"))
        if actual != expected:
            raise RuntimeError(f"Installed dependencies differ from {lock}; install the lock before building")
        record["dependencies"] = actual
        runner([python, "-m", "pip", "check"], logs / "dependency-check.log", cwd=root)
        if args.release:
            test_temp = work / "test-temp"
            test_temp.mkdir()
            environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                               LOCALAPPDATA=str(work / "test-user"), APPDATA=str(work / "test-roaming"),
                               XDG_STATE_HOME=str(work / "test-state"), TEMP=str(test_temp), TMP=str(test_temp), TMPDIR=str(test_temp))
            junit = logs / "source-tests.xml"
            runner([python, "-m", "pytest", "-q", f"--junitxml={junit}"], logs / "source-tests.log", cwd=root, environment=environment)
            record["source_tests"] = test_results(junit)
        package = candidate / name
        package.mkdir()
        record["frozen_binaries"] = {}
        for entry, executable, subsystem in (("D1ffler.py", "D1ffler.exe", "--windowed"), ("D1ffler_cli.py", "D1ffler-cli.exe", "--console")):
            resource = work / f"{executable}.version.txt"
            resource.write_text(version_resource(executable), encoding="utf-8")
            spec = work / f"{Path(executable).stem}.spec"
            spec.write_text(release_licensing.freeze_spec(root, entry, executable, resource, subsystem == "--windowed"), encoding="utf-8")
            command = [python, "-m", "PyInstaller", "--noconfirm", "--clean",
                       "--distpath", package, "--workpath", work / Path(executable).stem, spec]
            runner(command, logs / f"{executable}.log", cwd=root)
            output = package / executable
            if not output.is_file() or not output.stat().st_size:
                raise RuntimeError(f"Current build did not produce {executable}")
            record["frozen_binaries"][executable] = release_licensing.audit_binaries(work, executable)
        if not args.skip_smoke_tests:
            packaged_temp = work / "packaged-temp"
            packaged_temp.mkdir()
            environment = dict(os.environ, TEMP=str(packaged_temp), TMP=str(packaged_temp), TMPDIR=str(packaged_temp))
            report = logs / "packaged-tests.json"
            runner([python, root / "scripts" / "smoke_packaged.py", "--dist-dir", package, "--report", report, "--expected-version", APP_VERSION],
                   logs / "packaged-tests.log", cwd=root, environment=environment)
            record["packaged_tests"] = json.loads(report.read_text(encoding="utf-8"))
            if record["packaged_tests"].get("status") != "passed":
                raise RuntimeError("Packaged verification did not report success")
        record["licensing"] = release_licensing.assemble(root, candidate, package, record, inventory, dependency_sources)
        for filename in ("FIRST_RUN.txt", "CHANGELOG.md", "README.md"):
            if (root / filename).is_file():
                shutil.copy2(root / filename, package / filename)
        warning = "Locally built unsigned executables. Testing, signing, and any executable distribution are your responsibility.\n"
        (package / "USER_BUILD.txt").write_text(warning, encoding="utf-8")
        with (package / "FIRST_RUN.txt").open("a", encoding="utf-8") as stream:
            stream.write("\n" + warning + "See USER_BUILD.txt and README.md.\n")
        record["status"] = "candidate"
        record["artifacts"] = {item.relative_to(package).as_posix(): {"sha256": digest(item), "bytes": item.stat().st_size}
                               for item in sorted(package.rglob("*")) if item.is_file()}
        write_json(package / "release.json", record)
        checksums = "".join(f"{digest(item)}  {item.relative_to(package).as_posix()}\n" for item in sorted(package.rglob("*")) if item.is_file())
        (package / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
        # Only expose the ZIP filename after its payload has been re-read and verified.
        archive = candidate / f"{name}.zip.partial"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for item in sorted(package.rglob("*")):
                if item.is_file():
                    bundle.write(item, f"{name}/{item.relative_to(package).as_posix()}")
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise RuntimeError("Candidate ZIP failed integrity verification")
            for item in package.rglob("*"):
                if item.is_file() and hashlib.sha256(bundle.read(f"{name}/{item.relative_to(package).as_posix()}")).hexdigest() != digest(item):
                    raise RuntimeError("Candidate ZIP content differs from verified payload")
        archive = archive.rename(candidate / f"{name}.zip")
        source = record["licensing"]["source_archive"]
        (candidate / "SHA256SUMS.txt").write_text(f"{digest(archive)}  {archive.name}\n{source['sha256']}  {source['filename']}\n", encoding="utf-8")
        write_json(candidate / "release.json", record)
        print(f"Executable ZIP: {archive}\nCorresponding source: {candidate / source['filename']}\nTesting and signing these executables are your responsibility.", flush=True)
        return candidate
    except Exception as exc:
        record.update(status="failed", failure=str(exc))
        write_json(candidate / "release.json", record)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true", help="fresh environment, source suite, frozen checks, portable ZIP")
    parser.add_argument("--skip-install", action="store_true", help="local build only; requires exact locked packages")
    parser.add_argument("--skip-smoke-tests", action="store_true", help="local build only; candidate remains unverified")
    args = parser.parse_args(argv)
    try:
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
