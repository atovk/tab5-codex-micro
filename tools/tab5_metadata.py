"""USB-first metadata selection with one persistent BLE owner per user.

Only title_sync talks to hardware. chat_state_bridge writes its validated local
cache, so the two companions never create competing Bluetooth connections.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path

import tab5_transport


BLE_OWNER_PATH = Path.home() / "Library" / "Caches" / "Tab5 Codex Micro" / "metadata-ble.lock"


class MetadataTransport:
    def __init__(self, mode="auto", serial_number: str | None = None, *, ble_factory=None):
        if mode not in ("auto", "usb", "ble"):
            raise ValueError("Metadata transport must be auto, usb or ble")
        self.mode = mode
        self.serial_number = tab5_transport.selected_serial(serial_number)
        self.active = None
        self._ble_factory = ble_factory
        self._ble = None
        self._owner = None

    def _usb(self, requested):
        candidates = tab5_transport.application_ports(self.serial_number)
        if requested is not None:
            candidates = [device for device in candidates if device.device == requested]
        if len(candidates) > 1:
            raise RuntimeError("Multiple Tab5 USB interfaces require an explicit chip serial")
        if not candidates:
            if requested is not None:
                raise RuntimeError("The requested Tab5 USB interface is unavailable")
            return None
        target = candidates[0]
        self.serial_number = tab5_transport.selected_serial(target.serial_number)
        return target.device

    def _claim_owner(self):
        if self._owner is not None:
            return
        BLE_OWNER_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(BLE_OWNER_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EAGAIN, errno.EACCES):
                raise tab5_transport.SerialBusyError("Another companion owns Tab5 BLE metadata") from None
            raise
        self._owner = descriptor

    @contextmanager
    def connection(self, port: str | None = None, timeout: float = 0.5):
        if self.mode == "ble" and port is not None:
            raise ValueError("An explicit USB port cannot be combined with BLE transport")
        if self.mode != "ble":
            target = self._usb(port)
            if target is not None:
                # A busy or broken USB interface is not permission to fall back
                # to BLE. Hold/recheck its exact identity when opening the port.
                with tab5_transport.open_serial(target, timeout, serial_number=self.serial_number) as connection:
                    self.active = "usb"
                    yield connection
                return
            if self.mode == "usb":
                raise RuntimeError("The selected Tab5 USB interface is unavailable")
        self._claim_owner()
        if self._ble is None:
            if self._ble_factory is None:
                from tab5_ble import BleMetadataStream
                self._ble_factory = BleMetadataStream
            self._ble = self._ble_factory(self.serial_number)
        self._ble.connect()
        self.serial_number = self._ble.serial_number
        # Discovery/authentication can take seconds. USB may have arrived while
        # waiting, so check again before committing any wireless metadata.
        if self.mode == "auto":
            target = self._usb(None)
            if target is not None:
                with tab5_transport.open_serial(target, timeout, serial_number=self.serial_number) as connection:
                    self.active = "usb"
                    yield connection
                return
        with tab5_transport.serial_lock(timeout):
            if self.mode == "auto" and self._usb(None) is not None:
                raise tab5_transport.SerialBusyError("Tab5 USB arrived; retrying on the preferred transport")
            self.active = "ble"
            try:
                yield self._ble
            except TimeoutError:
                # An ATT write can complete while this client's notification
                # subscription is no longer delivering application ACKs. Do
                # not let physical HID connectivity mask a stuck GATT stream.
                self._ble.invalidate()
                raise
        # Intentionally retain GATT across heartbeats and USB takeover. A
        # second process cannot close this connection because of the owner lock.

    def close(self):
        try:
            if self._ble is not None:
                self._ble.close()
        finally:
            self._ble = None
            if self._owner is not None:
                fcntl.flock(self._owner, fcntl.LOCK_UN)
                os.close(self._owner)
                self._owner = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
