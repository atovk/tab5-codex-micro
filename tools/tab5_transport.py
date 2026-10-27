"""Serialized access to a Tab5 application CDC interface on this Mac."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import re
import time

import serial
from serial.tools import list_ports


VID = 0x303A
PID = 0x8360
SERIAL_PATTERN = re.compile(r"TAB5-[0-9A-F]{12}", re.IGNORECASE)
LOCK_PATH = Path.home() / "Library" / "Caches" / "Tab5 Codex Micro" / "serial.lock"


class SerialBusyError(TimeoutError):
    """Another Tab5 tool currently owns the CDC interface."""


@contextmanager
def serial_lock(timeout: float = 8):
    """Hold a per-user advisory lock shared by diagnostics, flashing and titles."""
    if timeout < 0:
        raise ValueError("Lock timeout must be nonnegative")
    LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise SerialBusyError("Tab5 serial interface is busy") from error
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def serial_port(requested: str | None = None) -> str:
    """Resolve one Tab5 application device; never select a ROM or S3.

    Multiple Tab5 devices require an explicit port or TAB5_SERIAL_NUMBER.
    The optional serial filter is read for each call, including services.
    """
    selected_serial = os.environ.get("TAB5_SERIAL_NUMBER") or None
    if selected_serial is not None and not SERIAL_PATTERN.fullmatch(selected_serial):
        raise ValueError("TAB5_SERIAL_NUMBER must have the form TAB5-0123456789AB")
    candidates = [
        port.device
        for port in list_ports.comports()
        if port.vid == VID and port.pid == PID
        and isinstance(port.serial_number, str)
        and SERIAL_PATTERN.fullmatch(port.serial_number)
        and (selected_serial is None or port.serial_number.upper() == selected_serial.upper())
        and (requested is None or port.device == requested)
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Expected one Tab5 application serial interface "
            f"({VID:04x}:{PID:04x}, TAB5 chip serial); found {len(candidates)}. "
            "Use --port or TAB5_SERIAL_NUMBER to select a device."
        )
    return candidates[0]


class LockedSerial(serial.Serial):
    def __init__(self, *args, lock_context=None, **kwargs):
        self._tab5_lock_context = lock_context
        super().__init__(*args, **kwargs)

    def close(self):
        try:
            super().close()
        finally:
            lock_context = self._tab5_lock_context
            self._tab5_lock_context = None
            if lock_context is not None:
                lock_context.__exit__(None, None, None)


def open_serial(port: str | None = None, timeout: float = 8) -> LockedSerial:
    """Open the exact Tab5 while holding its lock until connection.close()."""
    if timeout <= 0:
        raise ValueError("Serial timeout must be positive")
    lock_context = serial_lock(timeout)
    lock_context.__enter__()
    connection = None
    try:
        target = serial_port(port)
        connection = LockedSerial(
            port=None, baudrate=115200, timeout=min(0.2, timeout),
            write_timeout=timeout, lock_context=lock_context,
        )
        connection.dtr = True
        connection.rts = False
        connection.port = target
        connection.open()
        connection.reset_input_buffer()
        return connection
    except Exception:
        if connection is None:
            lock_context.__exit__(None, None, None)
        else:
            connection.close()
        raise
