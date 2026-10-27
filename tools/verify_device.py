#!/usr/bin/env python3
"""Read-only diagnostics and framebuffer capture for Tab5 Codex Micro.

Run with the project virtualenv:
  .venv/bin/python tools/verify_device.py discover
  .venv/bin/python tools/verify_device.py status
  .venv/bin/python tools/verify_device.py hid-status
  .venv/bin/python tools/verify_device.py screenshot artifacts/tab5.png

The HID check only requests device.status. It never generates key, joystick,
or lighting events. On macOS, HID devices are opened without seizing them.
"""

from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import re
import struct
import sys
import time

import hid
import serial
from serial.tools import list_ports
from tab5_transport import open_serial, serial_port


VID = 0x303A
PID = 0x8360
USAGE_PAGE = 0xFF00
REPORT_ID = 6
RPC_CHANNEL = 2
QUERY_ID = 9001


def hid_devices() -> list[dict]:
    return [
        device
        for device in hid.enumerate(VID, PID)
        if device.get("usage_page") == USAGE_PAGE
    ]


def printable_device(device: dict) -> dict:
    return {
        key: value.decode("utf-8", errors="backslashreplace")
        if isinstance(value, bytes)
        else value
        for key, value in device.items()
    }


def discover() -> dict:
    return {
        "hid": [printable_device(device) for device in hid_devices()],
        "serial": [
            {
                "port": port.device,
                "description": port.description,
                "vid": port.vid,
                "pid": port.pid,
                "serial_number": port.serial_number,
            }
            for port in list_ports.comports()
            if port.vid == VID
        ],
    }


def read_line(connection: serial.Serial, deadline: float) -> bytes:
    line = bytearray()
    while time.monotonic() < deadline:
        byte = connection.read(1)
        if not byte:
            continue
        if byte == b"\n":
            return bytes(line).rstrip(b"\r")
        line.extend(byte)
        if len(line) > 65536:
            raise RuntimeError("Serial diagnostic line exceeds 64 KiB")
    raise TimeoutError("Timed out waiting for a serial diagnostic line")


def status(port: str | None, timeout: float) -> dict:
    with open_serial(port, timeout) as connection:
        connection.write(b"STATUS\n")
        connection.flush()
        deadline = time.monotonic() + timeout
        observed = []
        while time.monotonic() < deadline:
            try:
                raw = read_line(connection, deadline)
            except TimeoutError:
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            observed.append(text)
            start = text.find("{")
            if start >= 0:
                try:
                    value = json.loads(text[start:])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and str(value.get("firmware", "")).startswith("tab5-micro-"):
                    return {"port": connection.port, "status": value}
            if text.startswith("STATUS "):
                return {"port": connection.port, "status": text[7:]}
        raise TimeoutError(f"No STATUS response. Serial lines: {observed[-8:]}")


def read_exact(connection: serial.Serial, count: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < count and time.monotonic() < deadline:
        data.extend(connection.read(min(65536, count - len(data))))
    if len(data) != count:
        raise TimeoutError(f"Incomplete framebuffer: {len(data)}/{count} bytes")
    return bytes(data)


def rgb565_image(data: bytes, width: int, height: int):
    from PIL import Image

    rgb = bytearray(width * height * 3)
    for index, (pixel,) in enumerate(struct.iter_unpack(">H", data)):
        red = (pixel >> 11) & 31
        green = (pixel >> 5) & 63
        blue = pixel & 31
        offset = index * 3
        rgb[offset] = (red << 3) | (red >> 2)
        rgb[offset + 1] = (green << 2) | (green >> 4)
        rgb[offset + 2] = (blue << 3) | (blue >> 2)
    return Image.frombytes("RGB", (width, height), bytes(rgb))


def screenshot(port: str | None, timeout: float, output: Path) -> dict:
    with open_serial(port, timeout) as connection:
        connection.write(b"SCREEN\n")
        connection.flush()
        deadline = time.monotonic() + timeout
        while True:
            header = read_line(connection, deadline)
            match = re.fullmatch(rb"FRAME (\d+) (\d+) RGB565_BE", header)
            if match:
                width, height = map(int, match.groups())
                break
        if not (1 <= width <= 4096 and 1 <= height <= 4096):
            raise RuntimeError(f"Invalid framebuffer dimensions: {width}x{height}")
        data = read_exact(connection, width * height * 2, deadline)
        trailer = b""
        for _ in range(4):
            trailer = read_line(connection, deadline)
            if trailer:
                break
        if trailer != b"END_FRAME":
            raise RuntimeError(f"Invalid framebuffer trailer: {trailer!r}")
        image = rgb565_image(data, width, height)
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG")
        return {
            "port": connection.port,
            "path": str(output),
            "width": width,
            "height": height,
            "format": "RGB565_BE",
            "frame_bytes": len(data),
        }


def nonexclusive_hid_open(path: bytes):
    if sys.platform == "darwin":
        library = ctypes.CDLL(hid.__file__)
        try:
            setter = library.hid_darwin_set_open_exclusive
        except AttributeError as error:
            raise RuntimeError(
                "This hidapi build cannot guarantee a nonexclusive macOS open"
            ) from error
        setter.argtypes = [ctypes.c_int]
        setter.restype = None
        setter(0)
    device = hid.device()
    try:
        device.open_path(path)
    except Exception:
        device.close()
        raise
    return device


def hid_status(timeout: float, serial_number: str | None) -> dict:
    candidates = hid_devices()
    if serial_number:
        candidates = [
            device
            for device in candidates
            if device.get("serial_number") == serial_number
        ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Expected one Codex Micro HID interface; use --serial-number to "
            f"select one. Found: {[printable_device(d) for d in candidates]}"
        )
    candidate = candidates[0]
    device = nonexclusive_hid_open(candidate["path"])
    try:
        request = json.dumps(
            {"method": "device.status", "params": None, "id": QUERY_ID},
            separators=(",", ":"),
        ).encode("utf-8")
        for offset in range(0, len(request), 61):
            chunk = request[offset : offset + 61]
            report = bytes([REPORT_ID, RPC_CHANNEL, len(chunk)]) + chunk
            report += bytes(64 - len(report))
            count = device.write(report)
            if count != len(report):
                raise RuntimeError(f"HID short write: {count}/{len(report)}")
        deadline = time.monotonic() + timeout
        pending = bytearray()
        while time.monotonic() < deadline:
            report = bytes(device.read(64, 200))
            if len(report) < 3 or report[0] != REPORT_ID:
                continue
            if report[1] != RPC_CHANNEL or report[2] > 61:
                continue
            if len(report) < report[2] + 3:
                continue
            pending.extend(report[3 : 3 + report[2]])
            if len(pending) > 65536:
                raise RuntimeError("HID response line exceeds 64 KiB")
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                try:
                    response = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(response, dict) or response.get("id") != QUERY_ID:
                    continue
                if "error" in response:
                    raise RuntimeError(f"device.status returned: {response['error']}")
                if not isinstance(response.get("result"), dict):
                    raise RuntimeError(f"Invalid device.status response: {response}")
                return {
                    "device": printable_device(candidate),
                    "response": response,
                }
        raise TimeoutError("No matching HID device.status response (id 9001)")
    finally:
        device.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Serial port; otherwise autodetect Tab5")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--serial-number", help="Select a specific HID device")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("discover", help="Enumerate candidate USB interfaces")
    subcommands.add_parser("status", help="Read serial STATUS diagnostics")
    subcommands.add_parser("hid-status", help="Query HID device.status only")
    capture = subcommands.add_parser("screenshot", help="Save framebuffer as PNG")
    capture.add_argument("output", type=Path, nargs="?", default=Path("artifacts/tab5.png"))
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        if args.command == "discover":
            result = discover()
        elif args.command == "status":
            result = status(args.port, args.timeout)
        elif args.command == "hid-status":
            result = hid_status(args.timeout, args.serial_number)
        else:
            result = screenshot(args.port, args.timeout, args.output)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
