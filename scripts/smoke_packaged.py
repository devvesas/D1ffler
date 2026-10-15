"""Smoke-test the Windows portable executables using only the Python standard library.

The driver needs Python; the copied executables run with no Python on PATH or in
their working directory. Also run this driver on a clean Windows machine before
release to verify that no installed runtime is masking a missing bundled file.
"""

from __future__ import annotations

import argparse
import ctypes
import csv
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def subsystem(executable):
    with executable.open("rb") as stream:
        require(stream.read(2) == b"MZ", f"Not a Windows executable: {executable}")
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        require(stream.read(4) == b"PE\0\0", f"Invalid PE header: {executable}")
        stream.seek(offset + 24 + 68)
        return struct.unpack("<H", stream.read(2))[0]


def check_cli(cli, root, environment):
    # An invalid Qt platform ensures a CLI invocation never constructs QApplication.
    environment = dict(environment, QT_QPA_PLATFORM="not_a_qt_platform")

    def invoke(arguments, code, *, stdout=subprocess.PIPE):
        result = subprocess.run([str(cli), *map(str, arguments)], cwd=root,
                                env=environment, stdout=stdout, stderr=subprocess.PIPE,
                                timeout=60)
        require(result.returncode == code,
                f"{arguments}: expected {code}, got {result.returncode}: "
                f"{result.stderr.decode('utf-8', errors='replace')}")
        return result

    left, right = root / "left café.txt", root / "right 日本語.txt"
    left.write_text("café 日本語\n", encoding="utf-8")
    right.write_bytes(left.read_bytes())
    original_left = left.read_bytes()
    same = invoke(["compare", left, right], 0)
    require(json.loads(same.stdout)["status"] == "identical", "Missing equivalent JSON report")
    require(same.stderr == b"", "Unexpected diagnostics on equivalent inputs")

    right.write_text("changed 日本語\n", encoding="utf-8")
    original_right = right.read_bytes()
    changed = invoke(["compare", left, right, "--progress"], 1)
    require(json.loads(changed.stdout)["different"], "Missing different JSON report")
    require(b"Compared 1/1 files" in changed.stderr, "Progress missing from stderr")
    diff = invoke(["compare", left, right, "--format", "diff"], 1).stdout.decode("utf-8")
    require("-café 日本語" in diff and "+changed 日本語" in diff,
            "Unicode unified report was damaged")

    # OS-level pipe consumption, as opposed to --output or a shell-decoded string.
    with subprocess.Popen([str(cli), "compare", str(left), str(right)], cwd=root,
                          env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        output, errors = process.communicate(timeout=60)
        require(process.returncode == 1 and not errors and json.loads(output)["different"],
                "Piped JSON report failed")
    redirected = root / "redirected report.json"
    with redirected.open("wb") as stream:
        invoke(["compare", left, right], 1, stdout=stream)
    require(json.loads(redirected.read_text(encoding="utf-8"))["different"],
            "Redirected JSON report failed")
    with subprocess.Popen([str(cli), "compare", str(left), str(right)], cwd=root,
                          env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        process.stdout.close()
        process.stdout = None
        _output, errors = process.communicate(timeout=60)
        require(process.returncode == 2 and b"Comparison failed:" in errors,
                "A closed report pipe did not return comparison failure (2)")

    for arguments in (["--help"], ["compare", "--help"]):
        help_result = invoke(arguments, 0)
        require(b"D1ffler-cli.exe" in help_result.stdout and not help_result.stderr,
                "Help must use the packaged command name and stdout")
    for arguments in (["compare"], ["compare", left, right, "--bad-option"],
                      ["unknown"], ["compare", left, root / "missing.txt"]):
        failure = invoke(arguments, 2)
        require(not failure.stdout and failure.stderr, "Invalid input must diagnose on stderr only")

    destination = root / "résultat 日本語.json"
    destination.write_text("previous report", encoding="utf-8")
    file_result = invoke(["compare", left, right, "--output", destination], 1)
    require(not file_result.stdout and not file_result.stderr, "--output polluted standard streams")
    require(json.loads(destination.read_text(encoding="utf-8"))["different"],
            "Existing report was not replaced")
    saved_report = destination.read_bytes()
    invoke(["compare", left, root / "missing.txt", "--output", destination], 2)
    require(destination.read_bytes() == saved_report, "Failed input replaced the existing report")

    # Deny sharing to force an actual replace failure in the frozen export engine.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(destination), 0x80000000, 0, None, 3, 0, None)
    require(handle != wintypes.HANDLE(-1).value, "Could not lock the report for the failure probe")
    try:
        blocked = invoke(["compare", left, right, "--output", destination], 2)
        require(not blocked.stdout and blocked.stderr, "Failed replacement did not diagnose correctly")
    finally:
        kernel.CloseHandle(handle)
    require(destination.read_bytes() == saved_report, "Failed replacement damaged the previous report")
    require(not list(root.glob(".D1ffler-export-*.tmp")), "Failed export left a temporary report")

    folders = [root / "folder left", root / "folder right"]
    for folder in folders:
        folder.mkdir()
        (folder / "café 日本語.txt").write_text("same\n", encoding="utf-8")
    require(json.loads(invoke(["compare", *folders], 0).stdout)["complete"],
            "Folder comparison did not complete")
    (folders[1] / "added.txt").write_text("new\n", encoding="utf-8")
    csv = invoke(["compare", *folders, "--format", "csv", "--progress"], 1)
    require("café 日本語.txt" in csv.stdout.decode("utf-8") and b"Compared" in csv.stderr,
            "Folder CSV or progress failed")
    inside = folders[0] / "report.json"
    invoke(["compare", *folders, "--output", inside], 2)
    require(not inside.exists(), "Output wrote into a compared source")
    corrupt = root / "corrupt.zip"
    corrupt.write_bytes(b"not a ZIP archive")
    incomplete = invoke(["compare", corrupt, folders[0], "--output", destination], 2)
    require(not incomplete.stdout and incomplete.stderr and destination.read_bytes() == saved_report,
            "Incomplete scan wrote a report")
    require(left.read_bytes() == original_left and right.read_bytes() == original_right,
            "CLI changed source files")
    print("Packaged CLI: exit codes, help, pipes, redirection, Unicode, progress, formats, atomic failures passed")


def find_gui_window(executable):
    """Find a visible main window owned by our uniquely copied GUI executable.

    One-file PyInstaller runs the app in a child process, so matching only the
    Popen PID would miss its window.
    """
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    found = []

    @callback_type
    def visit(window, _parameter):
        if not user.IsWindowVisible(window):
            return True
        title = ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(window, title, len(title))
        if not title.value.endswith(" — D1ffler"):
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = kernel.OpenProcess(0x1000, False, pid.value)
        if process:
            try:
                name = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(name))
                if (kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size))
                        and Path(name.value) == executable):
                    found.append(window)
            finally:
                kernel.CloseHandle(process)
        return True

    user.EnumWindows(visit, 0)
    return found[0] if found else None


def check_gui(gui, root, environment):
    environment = dict(environment)
    for key in ("QT_QPA_PLATFORM", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        environment.pop(key, None)
    process = subprocess.Popen([str(gui)], cwd=root, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        window = None
        while process.poll() is None and time.monotonic() < deadline:
            window = find_gui_window(gui)
            if window:
                break
            time.sleep(0.1)
        require(window is not None, "Packaged GUI did not show its main window")
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        require(user.PostMessageW(window, 0x0010, 0, 0), "Could not close the smoke-test GUI")
        output, errors = process.communicate(timeout=30)
        require(process.returncode == 0 and not output and not errors,
                f"GUI did not close cleanly: {process.returncode}, {errors!r}")
    finally:
        if process.poll() is None:
            subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=15, check=False)
            process.communicate(timeout=15)
    print("Packaged GUI: Windows GUI subsystem, visible main window, clean close passed")


def executable_version(executable):
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = version.GetFileVersionInfoSizeW(str(executable), None)
    require(size, f"Missing version metadata: {executable.name}")
    data = ctypes.create_string_buffer(size)
    require(version.GetFileVersionInfoW(str(executable), 0, size, data), "Could not read version metadata")
    value, length = ctypes.c_void_p(), wintypes.UINT()
    require(version.VerQueryValueW(data, r"\StringFileInfo\040904b0\ProductVersion", ctypes.byref(value), ctypes.byref(length)), "Missing ProductVersion")
    return ctypes.wstring_at(value, length.value - 1)


def check_gui_features(gui, upgrade_gui, root, environment, expected):
    results = []
    def probe(phase, probe_root, *, executable=gui, scale="1"):
        probe_root.mkdir(parents=True, exist_ok=True)
        report = probe_root / f"{phase}.json"
        env = dict(environment, LOCALAPPDATA=str(probe_root / "user"), APPDATA=str(probe_root / "roaming"), QT_SCALE_FACTOR=scale)
        for key in ("QT_QPA_PLATFORM", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_SCREEN_SCALE_FACTORS"):
            env.pop(key, None)
        # Testing-only baseline: QT_SCALE_FACTOR otherwise multiplies native DPI.
        env["QT_SCREEN_SCALE_FACTORS"] = "1"
        for key in ("QT_ENABLE_HIGHDPI_SCALING", "QT_FONT_DPI"):
            env.pop(key, None)
        result = subprocess.run([str(executable), "--verify-package", "--root", str(probe_root),
                                 "--report", str(report), "--phase", phase], cwd=root,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        require(result.returncode == 0 and data.get("status") == "passed", f"Frozen GUI {phase} at {scale}, exit {result.returncode}: {data}, {result.stderr!r}")
        require(data.get("version") == expected, "Frozen GUI application version differs from metadata")
        if phase == "exercise":
            require(abs(data["device_pixel_ratio"] - float(scale)) < 0.05, "Requested display scaling was not applied")
        results.append(data)
    for scale in ("1", "1.5", "2"):
        probe("exercise", root / f"scale-{scale}", scale=scale)
    probe("seed-recovery", root / "recovery")
    probe("restore", root / "recovery", executable=upgrade_gui)
    print("Packaged GUI: assets/help, file/folder/ZIP/image, edit/save, missing Git, 100/150/200% scaling, restart recovery and moved-install settings passed")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    parser.add_argument("--report", type=Path, help="Write the verification record")
    parser.add_argument("--expected-version", help="Require the release authority's version")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("The packaged smoke test requires Windows and an interactive desktop")
    report = {"schema_version": 1, "status": "failed", "clean_machine_certified": False,
              "skipped_checks": ["Testing on a clean Windows machine and manually inspecting multiple displays are the builder's responsibility"]}
    try:
        verify(args, report)
        report["status"] = "passed"
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if args.report:
            args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Both standalone executables passed packaged smoke tests.")


def verify(args, report):
    with tempfile.TemporaryDirectory(prefix="D1ffler-packaged-") as temporary:
        root = Path(temporary)
        install = root / "install café 日本語"
        install.mkdir()
        cli, gui = install / "D1ffler-cli.exe", install / "D1ffler.exe"
        for executable in (cli, gui):
            shutil.copy2(args.dist_dir / executable.name, executable)
        upgrade = root / "upgraded install 日本語"
        upgrade.mkdir()
        upgrade_gui = upgrade / gui.name
        shutil.copy2(gui, upgrade_gui)
        require(subsystem(cli) == 3, "CLI must use the Windows console subsystem")
        require(subsystem(gui) == 2, "GUI must use the Windows GUI subsystem")
        environment = dict(os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
                           LOCALAPPDATA=str(root / "user"), APPDATA=str(root / "roaming"),
                           PYTHONPATH="", PYTHONHOME="", PYTHONUTF8="1")
        version = executable_version(cli)
        require(version == executable_version(gui), "GUI and CLI metadata versions differ")
        if args.expected_version:
            require(version == args.expected_version, "Executable metadata differs from release version")
        result = subprocess.run([str(cli), "--version"], env=environment, capture_output=True, timeout=60)
        require(result.returncode == 0 and result.stdout.decode("utf-8").strip() == f"D1ffler {version}" and not result.stderr, "CLI --version differs from metadata")
        report["version"] = version
        system = Path(os.environ["SystemRoot"]) / "System32"
        identity = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        sid = next(csv.reader([identity.stdout.strip()]))[1]
        acl = system / "icacls.exe"
        locked = []
        try:
            for directory in (install, upgrade):
                # Generic W includes SYNCHRONIZE, also requested when launching
                # an executable. Deny the actual write rights while retaining it.
                subprocess.run([str(acl), str(directory), "/deny", f"*{sid}:(OI)(CI)(WD,AD,WEA,WA)"], capture_output=True, check=True)
                locked.append(directory)
                try:
                    (directory / "must-not-write.tmp").write_bytes(b"probe")
                except PermissionError:
                    pass
                else:
                    raise AssertionError("Read-only installation permission check did not deny writes")
            check_cli(cli, root, environment)
            check_gui(gui, root, environment)
            report["gui_probes"] = check_gui_features(gui, upgrade_gui, root, environment, version)
            report["checks"] = ["pe_subsystems", "consistent_version_metadata", "cli_reports_exit_codes_streams_unicode_atomic_output",
                                "read_only_installation_acl", "normal_gui_launch_close", "isolated_python_and_git_path"]
        finally:
            for directory in reversed(locked):
                subprocess.run([str(acl), str(directory), "/remove:d", f"*{sid}"], capture_output=True, check=True)


if __name__ == "__main__":
    main()
