#!/usr/bin/env python3
"""Manage the per-user Tab5 title companion without changing the desktop app.

Run ``python tools/title_sync_service.py install`` explicitly to start the
companion now and at login. ``stop`` unloads the current service; the plist
remains installed. Use ``stop --remove`` to also disable future login starts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile

from tab5_transport import selected_serial


LABEL = "com.tab5.codex-micro.titles"
WORKSPACE = Path(__file__).resolve().parent.parent
PYTHON = WORKSPACE / ".venv" / "bin" / "python"
COMPANION = WORKSPACE / "tools" / "title_sync.py"
LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "Tab5 Codex Micro"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LAUNCHCTL = "/bin/launchctl"


class ServiceError(RuntimeError):
    pass


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    if sys.platform != "darwin":
        raise ServiceError("This service helper requires macOS launchctl.")
    try:
        return subprocess.run(
            [LAUNCHCTL, *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceError(f"Could not run launchctl: {error}") from error


def result_error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"launchctl exited {result.returncode}"


def is_absent(result: subprocess.CompletedProcess[str]) -> bool:
    message = (result.stderr + result.stdout).lower()
    return any(
        text in message
        for text in ("could not find service", "service could not be found", "no such process")
    )


def loaded_info() -> dict:
    result = launchctl("print", target())
    if result.returncode:
        if is_absent(result):
            return {"loaded": False}
        raise ServiceError(f"Could not inspect title service: {result_error(result)}")
    info: dict = {"loaded": True}
    # Avoid exposing launchctl's full environment dump; only report useful
    # lifecycle fields from this helper's own service.
    for name, pattern in (
        ("state", r"^\s*state = (.+)$"),
        ("pid", r"^\s*pid = (\d+)$"),
        ("runs", r"^\s*runs = (\d+)$"),
        ("last_exit_code", r"^\s*last exit code = (-?\d+)$"),
    ):
        match = re.search(pattern, result.stdout, re.MULTILINE)
        if match:
            info[name] = match.group(1) if name == "state" else int(match.group(1))
    return info


def paths() -> dict:
    return {
        "label": LABEL,
        "target": target(),
        "workspace": str(WORKSPACE),
        "python": str(PYTHON),
        "companion": str(COMPANION),
        "plist": str(PLIST_PATH),
        "stdout_log": str(LOG_DIRECTORY / "titles.stdout.log"),
        "stderr_log": str(LOG_DIRECTORY / "titles.stderr.log"),
    }


def unload() -> bool:
    if not loaded_info()["loaded"]:
        return False
    result = launchctl("bootout", target())
    if result.returncode and not is_absent(result):
        raise ServiceError(f"Could not unload title service: {result_error(result)}")
    return result.returncode == 0


def service_plist(mode: str = "auto", serial_number: str | None = None) -> dict:
    """Construct the one hardware-owner service; the IPC bridge stays cache-only."""
    if mode not in ("auto", "usb", "ble"):
        raise ValueError("Metadata transport must be auto, usb or ble")
    identity = selected_serial(serial_number)
    arguments = [str(PYTHON), str(COMPANION), "--watch", "--transport", mode]
    if identity is not None:
        arguments.extend(("--device-serial", identity))
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(WORKSPACE),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Nice": 10,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(LOG_DIRECTORY / "titles.stdout.log"),
        "StandardErrorPath": str(LOG_DIRECTORY / "titles.stderr.log"),
    }


def install(mode: str = "auto", serial_number: str | None = None) -> dict:
    plist = service_plist(mode, serial_number)
    if not PYTHON.is_file() or not os.access(PYTHON, os.X_OK):
        raise ServiceError(f"The workspace Python interpreter is missing or not executable: {PYTHON}")
    if not COMPANION.is_file():
        raise ServiceError(f"The title companion is missing: {COMPANION}")
    # Check the user's GUI launch domain before writing anything.
    loaded_info()
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{LABEL}.", suffix=".plist", dir=PLIST_PATH.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            plistlib.dump(plist, stream, fmt=plistlib.FMT_XML, sort_keys=True)
        temporary.chmod(0o644)
        temporary.replace(PLIST_PATH)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    unload()
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH))
    if result.returncode:
        raise ServiceError(f"Could not start title service: {result_error(result)}")
    return {"action": "installed", **paths(), "plist_exists": True, **loaded_info()}


def stop(remove: bool = False) -> dict:
    unloaded = unload()
    if remove:
        PLIST_PATH.unlink(missing_ok=True)
    return {
        "action": "removed" if remove else "stopped",
        **paths(),
        "was_loaded": unloaded,
        "plist_exists": PLIST_PATH.exists(),
        **loaded_info(),
    }


def status() -> dict:
    return {
        **paths(),
        "plist_exists": PLIST_PATH.exists(),
        "python_exists": PYTHON.is_file(),
        "companion_exists": COMPANION.is_file(),
        **loaded_info(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="Start the title companion now and automatically at login.")
    install_parser.add_argument("--transport", choices=("auto", "usb", "ble"), default="auto")
    install_parser.add_argument("--device-serial", help="Persist the exact P4 USB/BLE chip identity")
    stop_parser = commands.add_parser("stop", help="Unload the title companion; absent services are accepted.")
    stop_parser.add_argument("--remove", action="store_true", help="Also remove its login LaunchAgent plist.")
    commands.add_parser("status", help="Print JSON service state and stable paths.")
    arguments = parser.parse_args()
    try:
        if arguments.command == "install":
            result = install(arguments.transport, arguments.device_serial)
        elif arguments.command == "stop":
            result = stop(arguments.remove)
        else:
            result = status()
    except (ServiceError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), **paths()}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
