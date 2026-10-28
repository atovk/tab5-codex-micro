#!/usr/bin/env python3
"""Build and flash Tab5, including its USB-C software download-mode recovery."""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

import serial
from serial.tools import list_ports
from tab5_transport import serial_lock
from build_title_font import inspect_asset
from flash_release import (APP_PID, ROM_PID, VID, choose_port, enter_rom,
                           firmware_version, inspect_firmware, is_application)

ROOT = Path(__file__).resolve().parents[1]


def run(args, *, capture_output=False):
    return subprocess.run([str(x) for x in args], cwd=ROOT, check=True,
                          capture_output=capture_output, text=capture_output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='Tab5 serial port; required for ROM or multiple devices')
    parser.add_argument('--skip-build', action='store_true', help='Flash already-built firmware images')
    images = parser.add_mutually_exclusive_group()
    images.add_argument('--app-only', action='store_true',
                        help='Update only firmware.bin at 0x10000, preserving NVS, fonts and settings')
    images.add_argument('--font-image', type=Path,
                        help='Also write a validated T5VLW4 title font to the reserved data partition')
    args = parser.parse_args(argv)
    pio = ROOT / '.venv/bin/pio'
    if not args.skip_build:
        run([sys.executable, ROOT / 'tools/prepare_platform.py'])
        run([pio, 'run'])
    image = ROOT / '.pio/build/tab5/firmware.factory.bin'
    if not image.is_file():
        raise RuntimeError('Build the Tab5 firmware before flashing')
    if args.font_image:
        args.font_image = args.font_image.expanduser().resolve()
        metadata = inspect_asset(args.font_image.read_bytes())
        print(f"Validated title font: {metadata['glyph_count']} glyphs, {metadata['file_size']} bytes", flush=True)
    with serial_lock(10):
        flash(args, image)


def rom_application_serial(port):
    """Identify the selected P4 before writing; its base MAC is the app serial."""
    result = run([sys.executable, '-m', 'esptool', '--chip', 'esp32p4', '--port', port,
                  '--before', 'default-reset', '--after', 'no-reset', '--no-stub', 'read-mac'],
                 capture_output=True)
    macs = set(re.findall(r'^\s*(?:BASE )?MAC\s*:\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s*$',
                          result.stdout, re.IGNORECASE | re.MULTILINE))
    macs = {mac.replace(':', '').upper() for mac in macs}
    if len(macs) != 1:
        raise RuntimeError('Could not identify the selected ESP32-P4 base MAC; no flash writes were made')
    return 'TAB5-' + macs.pop()


def read_firmware_status(port, timeout):
    """Request a fresh version without retaining or printing private STATUS fields.

    The caller holds the shared serial lock across reset and verification. Opening
    here must not acquire that same lock a second time.
    """
    connection = serial.Serial(port=None, baudrate=115200, timeout=min(0.2, timeout),
                               write_timeout=min(2, timeout))
    deadline = time.monotonic() + timeout
    try:
        connection.dtr, connection.rts, connection.port = True, False, port
        connection.open()
        connection.reset_input_buffer()
        connection.write(b'STATUS\n')
        connection.flush()
        line = bytearray()
        while time.monotonic() < deadline:
            byte = connection.read(1)
            if not byte:
                continue
            if byte != b'\n':
                line.extend(byte)
                if len(line) > 65536:
                    raise RuntimeError('Firmware verification response exceeds 64 KiB')
                continue
            start = line.find(b'{')
            if start >= 0:
                try:
                    value = json.loads(line[start:])
                except (ValueError, UnicodeDecodeError):
                    pass
                else:
                    if isinstance(value, dict) and isinstance(value.get('firmware'), str):
                        return value['firmware']
            line.clear()
        raise TimeoutError('No firmware STATUS response from the flashed device')
    finally:
        connection.close()


def wait_for_application(expected_serial, version, timeout=25):
    """Only the selected chip with the flashed version can satisfy reconnect."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [port for port in list_ports.comports() if is_application(port)
                   and port.serial_number.upper() == expected_serial.upper()]
        if len(matches) > 1:
            raise RuntimeError('Several application ports claim the flashed device identity')
        if matches:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                reported = read_firmware_status(matches[0].device, min(3, remaining))
            except (OSError, TimeoutError, serial.SerialException):
                # Enumeration may precede CDC readiness. Retry only this chip.
                pass
            else:
                if reported != 'tab5-micro-' + version:
                    raise RuntimeError('The flashed device did not report the expected firmware version ' + version)
                print('Tab5 Codex Micro verified: ' + matches[0].device + ' (' + version + ')', flush=True)
                return matches[0].device
        time.sleep(0.3)
    raise RuntimeError('Flash writes completed, but the same Tab5 did not reconnect with verified firmware STATUS')


def inspect_application(factory_data, application_data):
    """Check the application against an already-validated complete factory image.

    Incremental updates cannot install a new partition layout. Require exactly
    the project's factory app partition, and no ambiguous second app partition.
    The binary must equal the entire app payload, not merely a matching prefix.
    """
    applications = []
    for at in range(0x8000, 0x8C00, 32):
        magic, kind, subtype, offset, size = struct.unpack_from('<HBBII', factory_data, at)
        if magic in (0xFFFF, 0xEBEB):
            break
        if magic != 0x50AA:
            raise RuntimeError('Invalid factory partition table for application-only update')
        if kind == 0:
            applications.append((subtype, offset, size))
    if applications != [(0, 0x10000, 0x500000)]:
        raise RuntimeError('Application-only update requires one factory app partition at 0x10000')
    _, offset, capacity = applications[0]
    if not 24 <= len(application_data) <= capacity:
        raise RuntimeError('Application image is empty, truncated, or exceeds its partition')
    if application_data[0] != 0xE9 or struct.unpack_from('<H', application_data, 12)[0] != 18:
        raise RuntimeError('Application image is not an ESP32-P4 image')
    if application_data != factory_data[offset:]:
        raise RuntimeError('Application image does not exactly match the validated factory app payload')
    return offset


def flash(args, image):
    app_only = getattr(args, 'app_only', False)
    if app_only and args.font_image:
        raise RuntimeError('--app-only cannot be combined with --font-image')
    data = image.read_bytes()
    version = firmware_version(data)
    inspect_firmware(data, version)
    write_image, write_offset = image, '0'
    if app_only:
        write_image = image.with_name('firmware.bin')
        if not write_image.is_file():
            raise RuntimeError('Build firmware.bin before using --app-only')
        write_offset = hex(inspect_application(data, write_image.read_bytes()))
        print('Validated application-only update: NVS, title fonts and settings will be preserved', flush=True)
    target = choose_port(list(list_ports.comports()), args.port)
    previous_serial = target.serial_number.upper() if is_application(target) else None
    port = enter_rom(target, serial, list_ports.comports)
    expected_serial = rom_application_serial(port)
    if previous_serial is not None and expected_serial != previous_serial:
        raise RuntimeError('The ROM chip identity differs from the selected Tab5; no flash writes were made')
    # Keep the P4 ROM downloader active through the uploader's final reset.
    base = [sys.executable, '-m', 'esptool', '--chip', 'esp32p4', '--port', port]
    run(base + ['--before', 'no-reset', '--after', 'no-reset',
                'write-mem', '0x50110008', '0x4', '0x4'])
    write = base + ['--baud', '1500000', '--before', 'no-reset', '--after', 'no-reset',
                    'write-flash', write_offset, write_image]
    if args.font_image:
        write += ['0x510000', args.font_image]
    run(write)
    run(base + ['--before', 'no-reset', '--after', 'watchdog-reset',
                'write-mem', '0x50110008', '0x0', '0x4'])
    return wait_for_application(expected_serial, version)


if __name__ == '__main__':
    main()
