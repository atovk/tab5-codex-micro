#!/usr/bin/env python3
"""Flash an extracted Tab5 release on macOS/Linux, without a source checkout.

Use --check for an offline integrity check with only the Python standard
library. Flashing additionally requires the bundled requirements-flash.txt.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import time
import zlib


FORMAT = "tab5-codex-micro-release"
FLASH_BYTES = 0x1000000
FONT_OFFSET, FONT_CAPACITY = 0x510000, 0xAE0000
VID, APP_PID, ROM_PID = 0x303A, 0x8360, 0x1001
APP_SERIAL = re.compile(r"TAB5-[0-9A-F]{12}", re.IGNORECASE)
REQUIRED_FILES = frozenset(("manifest.json", "firmware.factory.bin", "title-font.bin",
                           "flash.py", "requirements-flash.txt", "FLASHING.md",
                           "FLASHING.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
                           "licenses/Tab5-UI-Sans-OFL.txt", "licenses/TinyUSB-MIT.txt"))


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bundle:
    directory: Path
    version: str
    firmware: Path
    font: Path
    glyph_count: int


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def relative_file(directory: Path, name: str) -> Path:
    parts = PurePosixPath(name)
    if (not name or "\\" in name or parts.is_absolute() or ".." in parts.parts
            or name != parts.as_posix()):
        raise ReleaseError("Unsafe release filename")
    result = directory.joinpath(*parts.parts)
    if result.is_symlink() or not result.resolve().is_relative_to(directory.resolve()):
        raise ReleaseError("Release file escapes its directory")
    if not result.is_file():
        raise ReleaseError("Missing release file: " + name)
    return result


def inspect_font(data: bytes) -> int:
    """Validate the same fixed native-24px packed VLW format as firmware."""
    if len(data) < 64 or len(data) > FONT_CAPACITY or data[:8] != b"T5VLW4\0\0":
        raise ReleaseError("Invalid title font magic/size")
    version, size, raw, bitmap, count, crc = struct.unpack_from("<6I", data, 8)
    if (version != 1 or size != len(data) or not 0 < count <= 65535
            or bitmap != 24 + count * 28 or raw < bitmap or raw > 0x7FFFFFFF
            or size != 64 + bitmap + (raw - bitmap + 1) // 2 or any(data[32:64])):
        raise ReleaseError("Invalid title font header/bounds")
    payload = data[64:]
    if zlib.crc32(payload) != crc:
        raise ReleaseError("Title font CRC mismatch")
    actual, vlw_version, pixelsize, reserved, ascent, descent = struct.unpack_from(">6I", payload)
    if ((actual, vlw_version, pixelsize, reserved) != (count, 11, 24, 0)
            or not 0 <= ascent < 64 or not 0 <= descent < 64 or not 0 < ascent + descent < 64):
        raise ReleaseError("Invalid title font VLW header")
    previous, pixels = -1, 0
    for index in range(count):
        code, height, width, advance, top, left, flags = struct.unpack_from(">7i", payload, 24 + index * 28)
        if (not previous < code <= 0xFFFF or 0xD800 <= code <= 0xDFFF
                or not 0 < height <= 64 or not 0 < width <= 64 or not 0 <= advance <= 64
                or not -64 <= top <= 64 or not -64 <= left <= 64 or flags not in (0, 1)):
            raise ReleaseError("Invalid title font glyph metrics")
        previous = code
        pixels += height * width
    if pixels != raw - bitmap or (pixels % 2 and data[-1] & 15):
        raise ReleaseError("Invalid title font bitmap bounds")
    return count


def firmware_version(data: bytes) -> str:
    """Read a complete firmware identifier, never a prefix of another version."""
    versions = set(re.findall(
        rb"(?<![0-9A-Za-z_.+-])tab5-micro-([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)\x00",
        data[0x10000:]))
    if len(versions) != 1:
        raise ReleaseError("Factory image must contain one unambiguous terminated firmware version")
    return versions.pop().decode("ascii")


def inspect_firmware(data: bytes, version: str) -> None:
    if not 0x10018 <= len(data) <= FONT_OFFSET:
        raise ReleaseError("Factory image is empty, truncated, or overlaps the font partition")
    for offset in (0x2000, 0x10000):
        if data[offset] != 0xE9 or struct.unpack_from("<H", data, offset + 12)[0] != 18:
            raise ReleaseError("Factory image is not an ESP32-P4 image")
    if firmware_version(data) != version:
        raise ReleaseError("Factory image version does not match the release")
    partitions = set()
    for at in range(0x8000, 0x8C00, 32):
        magic, kind, subtype, offset, size = struct.unpack_from("<HBBII", data, at)
        if magic == 0x50AA:
            partitions.add((kind, subtype, offset, size))
        elif magic in (0xFFFF, 0xEBEB):
            break
        else:
            raise ReleaseError("Invalid factory partition table")
    if not {(0, 0, 0x10000, 0x500000), (1, 0x82, FONT_OFFSET, FONT_CAPACITY)} <= partitions:
        raise ReleaseError("Factory partition layout does not match this release")


def validate_bundle(directory: Path) -> Bundle:
    directory = directory.resolve()
    sums_path = relative_file(directory, "SHA256SUMS")
    if sums_path.stat().st_size > 64 * 1024:
        raise ReleaseError("Checksum list is too large")
    sums = {}
    for line in sums_path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] in sums:
            raise ReleaseError("Malformed or duplicate checksum entry")
        checksum, name = match.groups()
        target = relative_file(directory, name)
        if target.stat().st_size > 32 * 1024 * 1024:
            raise ReleaseError("Release file is unexpectedly large: " + name)
        if digest(target) != checksum:
            raise ReleaseError("SHA-256 mismatch: " + name)
        sums[name] = checksum
    if not REQUIRED_FILES <= sums.keys():
        raise ReleaseError("Checksum list omits required release files")
    manifest_path = relative_file(directory, "manifest.json")
    if manifest_path.stat().st_size > 16 * 1024:
        raise ReleaseError("Manifest is too large")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or type(manifest.get("schema")) is not int or manifest["schema"] != 1 or manifest.get("chip") != "esp32p4"
            or manifest.get("board") != "M5Stack Tab5" or manifest.get("flash_bytes") != FLASH_BYTES):
        raise ReleaseError("Unsupported release manifest or hardware target")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ReleaseError("Invalid release version")
    images = manifest.get("images")
    if not isinstance(images, list) or len(images) != 2:
        raise ReleaseError("Manifest must contain the two release images")
    expected = {"firmware.factory.bin": 0, "title-font.bin": FONT_OFFSET}
    seen = set()
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("file"), str) or image["file"] not in expected:
            raise ReleaseError("Unexpected image filename")
        name = image["file"]
        path = relative_file(directory, name)
        if (name in seen or type(image.get("offset")) is not int or image["offset"] != expected[name]
                or type(image.get("bytes")) is not int or image["bytes"] != path.stat().st_size
                or image.get("sha256") != sums[name]):
            raise ReleaseError("Image manifest does not match its data/range")
        seen.add(name)
    firmware = relative_file(directory, "firmware.factory.bin")
    font = relative_file(directory, "title-font.bin")
    inspect_firmware(firmware.read_bytes(), version)
    count = inspect_font(font.read_bytes())
    if manifest.get("font") != {"format": "T5VLW4", "pixels": 24, "glyphs": count}:
        raise ReleaseError("Font manifest does not match its data")
    return Bundle(directory, version, firmware, font, count)


def is_application(port) -> bool:
    return (port.vid == VID and port.pid == APP_PID
            and bool(APP_SERIAL.fullmatch(port.serial_number or "")))


def choose_port(ports: list, requested: str | None):
    if requested:
        found = [port for port in ports if port.device == requested and port.vid == VID
                 and (port.pid == ROM_PID or is_application(port))]
        if len(found) == 1:
            return found[0]
        raise ReleaseError("The selected port is not a Tab5 Micro application or Espressif ROM port")
    espressif = [port for port in ports if port.vid == VID]
    if len(espressif) == 1 and is_application(espressif[0]):
        return espressif[0]
    raise ReleaseError("Specify --port explicitly; ROM ports and multiple devices are never auto-selected")


@contextmanager
def serial_lock(timeout: float = 30):
    if os.name != "posix":
        raise ReleaseError("Flashing is supported on macOS/Linux; --check works on other platforms")
    import errno
    import fcntl
    lock = Path.home() / "Library/Caches/Tab5 Codex Micro/serial.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while not acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise ReleaseError("The Tab5 serial port is busy; stop other diagnostics and retry") from error
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def enter_rom(port, serial_module, ports_provider) -> str:
    if not is_application(port):
        return port.device
    existing = {item.device for item in ports_provider() if item.vid == VID and item.pid == ROM_PID}
    print("Requesting Tab5 download mode...", flush=True)
    connection = serial_module.Serial(port=None, baudrate=115200, timeout=1, write_timeout=2)
    try:
        connection.dtr, connection.rts, connection.port = True, False, port.device
        connection.open()
        connection.write(b"BOOTLOADER\n")
        connection.flush()
        time.sleep(0.3)
    finally:
        connection.close()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        new = [item for item in ports_provider() if item.vid == VID and item.pid == ROM_PID
               and item.device not in existing]
        if len(new) == 1:
            old_location, new_location = getattr(port, "location", None), getattr(new[0], "location", None)
            if old_location and new_location and old_location != new_location:
                raise ReleaseError("New ROM port appeared on a different USB connection; select the correct port")
            return new[0].device
        if len(new) > 1:
            raise ReleaseError("Several new ROM ports appeared; rerun with the correct --port")
        time.sleep(0.3)
    raise ReleaseError("No Tab5 ROM port appeared; hold Reset for about two seconds, then select its port")


def flash_verified(bundle: Bundle, port: str, skip_font: bool = False, runner=subprocess.run) -> None:
    base = [sys.executable, "-m", "esptool", "--chip", "esp32p4", "--port", port]

    def run(arguments):
        runner(base + arguments, check=True)

    # Explicit chip detection must succeed before any target memory write.
    # The shared ROM VID/PID alone cannot distinguish an ESP32-P4 from an S3.
    run(["--before", "default-reset", "--after", "no-reset", "chip-id"])
    run(["--before", "no-reset", "--after", "no-reset", "write-mem", "0x50110008", "0x4", "0x4"])
    write = ["--baud", "1500000", "--before", "no-reset", "--after", "no-reset",
             "write-flash", "0x0", str(bundle.firmware)]
    if not skip_font:
        write += ["0x510000", str(bundle.font)]
    run(write)
    run(["--before", "no-reset", "--after", "watchdog-reset", "write-mem", "0x50110008", "0x0", "0x4"])
    print("Flash verified and reset requested. Open Codex Settings > Codex Micro after the device restarts.", flush=True)


def dependencies():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as error:
        raise ReleaseError("Install requirements-flash.txt before accessing USB") from error
    return serial, list_ports.comports


def main(argv=None) -> int:
    if sys.version_info < (3, 11):
        raise ReleaseError("Use Python 3.11 or newer")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="Explicit Tab5 application or ROM serial port")
    parser.add_argument("--list-ports", action="store_true", help="List Espressif USB ports; does not open them")
    parser.add_argument("--check", action="store_true", help="Validate the complete release offline; does not open USB")
    parser.add_argument("--skip-font", action="store_true", help="Update firmware while retaining an already-installed font")
    args = parser.parse_args(argv)
    if args.check and args.list_ports:
        parser.error("--check and --list-ports are separate operations")
    if args.list_ports:
        _, provider = dependencies()
        print(json.dumps([{"port": port.device, "description": port.description,
                           "vid": port.vid, "pid": port.pid, "serial_number": port.serial_number,
                           "micro_application": is_application(port)}
                          for port in provider() if port.vid == VID], indent=2))
        return 0
    bundle = validate_bundle(Path(__file__).resolve().parent)
    print(f"Release {bundle.version} verified: ESP32-P4 firmware + {bundle.glyph_count:,} title glyphs", flush=True)
    if args.check:
        return 0
    serial_module, provider = dependencies()
    with serial_lock():
        target = choose_port(list(provider()), args.port)
        rom = enter_rom(target, serial_module, provider)
        flash_verified(bundle, rom, args.skip_font)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Release flash failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
