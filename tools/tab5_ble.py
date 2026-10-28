"""One persistent BLE metadata stream for the Tab5 companion.

This is the private metadata GATT service, not the native Micro HID service.
Native key/encoder/joystick events and lights remain owned by the desktop app.
Bleak is imported only when a wireless connection is actually requested.
The real firmware requires encrypted, MITM-authenticated GATT access. Matching
its public serial is not cryptographic authentication of an arbitrary server;
an active peripheral impersonating that service is outside this protection.
"""

from __future__ import annotations

import asyncio
from functools import partial
import json
import re
import sys
import time
from types import SimpleNamespace

from tab5_transport import selected_serial


SERVICE_UUID = "8f4d1000-7d4b-4b58-9e53-5e4ad07e7a50"
RX_UUID = "8f4d1001-7d4b-4b58-9e53-5e4ad07e7a50"
TX_UUID = "8f4d1002-7d4b-4b58-9e53-5e4ad07e7a50"
IDENTITY_UUID = "8f4d1003-7d4b-4b58-9e53-5e4ad07e7a50"
MAX_LINE = 6144
MAX_REPLY = 8192
AUTH_TIMEOUT_SECONDS = 60.0


def _corebluetooth_client_backend():
    """Extend only Identity reads through Bleak's public backend override.

    Pinned Bleak 3.0.1 has a separate 20-second delegate read timeout and its
    public read method does not forward a timeout argument. An outer wait_for
    alone therefore cannot give a person 60 seconds to enter the pairing PIN.
    No installed backend files or global delegate methods are modified.
    """
    from bleak.backends.corebluetooth.client import BleakClientCoreBluetooth

    class MetadataCoreBluetoothClient(BleakClientCoreBluetooth):
        async def read_gatt_char(self, characteristic, *, use_cached=False, **kwargs):
            if characteristic.uuid.lower() != IDENTITY_UUID:
                return await super().read_gatt_char(
                    characteristic, use_cached=use_cached, **kwargs)
            if self._delegate is None:
                raise RuntimeError("Tab5 BLE peripheral delegate is unavailable")
            value = await self._delegate.read_characteristic(
                characteristic.obj, use_cached=use_cached, timeout=AUTH_TIMEOUT_SECONDS)
            return bytearray(value)

    return MetadataCoreBluetoothClient


def _failure_detail(error: Exception) -> str:
    """Expose bounded diagnostics, never raw errors containing peer identities."""
    if isinstance(error, TimeoutError):
        return "timeout"
    # Bleak wraps Apple's NSError in prose. Retain only its fixed domain and
    # integer code; omit descriptions, UUIDs, PINs and characteristic values.
    match = re.search(r"\b(CBATTErrorDomain|CBErrorDomain) Code=(-?[0-9]{1,6})(?![0-9])", str(error))
    if match:
        return f"{match[1]}:{int(match[2])}"
    if isinstance(error, ValueError):
        return "invalid-data"
    if isinstance(error, OSError):
        return "os-error"
    return "backend-error"


async def _connected_peripherals() -> dict:
    """Find macOS HID-connected devices that have stopped advertising.

    CoreBluetooth's retrieval API is public. The small BLEDevice adapter follows
    the pinned Bleak 3.0.1 CoreBluetooth backend's (peripheral, manager) details
    contract; no other process's connection is disconnected or unpaired.
    """
    if sys.platform != "darwin":
        return {}
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice
    from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
    manager = CentralManagerDelegate()
    await asyncio.wait_for(manager.wait_until_ready(), 5.0)
    peripherals = manager.central_manager.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)])
    result = {}
    for peripheral in peripherals:
        address = str(peripheral.identifier().UUIDString())
        device = BLEDevice(address, peripheral.name(), (peripheral, manager))
        advertisement = SimpleNamespace(service_uuids=[SERVICE_UUID],
                                         service_data={}, manufacturer_data={})
        result[address.upper()] = (device, advertisement)
    return result


class MetadataScanner:
    @staticmethod
    async def discover(**options):
        from bleak import BleakScanner
        connected = await _connected_peripherals()
        try:
            advertising = await BleakScanner.discover(**options)
        except Exception:
            # A system HID connection can remain usable when active scanning
            # is unavailable. Selection still rejects ambiguity and checks the
            # returned chip serial before sending metadata. Encryption is
            # enforced by the real firmware, not proved by this serial check.
            if connected:
                return connected
            raise
        # Advertisement hints refine a connected device, never create a second
        # candidate for the same CoreBluetooth UUID.
        return {**connected, **{address.upper(): item for address, item in advertising.items()}}


def decode_identity(raw: bytes, expected: str | None = None) -> str:
    """Match the public P4 serial; this does not authenticate a hostile server."""
    if len(raw) > 512:
        raise ValueError("Tab5 BLE identity exceeds its size limit")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Tab5 BLE identity is invalid") from None
    if (not isinstance(value, dict) or type(value.get("v")) is not int
            or value["v"] != 1 or type(value.get("maxLine")) is not int
            or value["maxLine"] != MAX_LINE or not isinstance(value.get("serial"), str)):
        raise ValueError("Tab5 BLE metadata protocol is incompatible")
    identity = selected_serial(value["serial"])
    if identity is None or (expected is not None and identity != selected_serial(expected)):
        raise ValueError("Tab5 BLE identity does not match the selected chip")
    return identity


def choose_candidate(discovered: dict, expected: str | None = None):
    """Never probe multiple unknown peripherals or trust their advertised name.

    Optional service-data serials only narrow discovery. The Identity response
    must still match before any title or chat state is transmitted. The real
    firmware protects that read with encryption and MITM authentication; public
    serial matching alone does not prevent an active service impersonator.
    """
    expected = selected_serial(expected)
    candidates = []
    for device, advertisement in discovered.values():
        if SERVICE_UUID not in {uuid.lower() for uuid in advertisement.service_uuids}:
            continue
        hints = {uuid.lower(): data for uuid, data in advertisement.service_data.items()}
        hint = hints.get(SERVICE_UUID)
        if hint is None:
            manufacturer_hint = advertisement.manufacturer_data.get(0x303A)
            if manufacturer_hint is not None and len(manufacturer_hint) == 6:
                hint = ("TAB5-" + bytes(manufacturer_hint).hex().upper()).encode("ascii")
        if expected is not None and hint is not None:
            try:
                if selected_serial(bytes(hint).decode("ascii")) != expected:
                    continue
            except (ValueError, UnicodeError):
                continue
        candidates.append(device)
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one matching Tab5 BLE metadata peripheral")
    return candidates[0]


class BleMetadataStream:
    """Synchronous line I/O over one retained asyncio/Bleak connection.

    Re-entering the context does not disconnect GATT. A caller must own the
    cross-process BLE lock for this object's whole lifetime, including USB use.
    No automatic pair/unpair operation is issued; macOS handles authentication
    when the real firmware's protected Identity characteristic is read. The
    companion has no application-layer proof against a cloned GATT service.
    """

    def __init__(self, serial_number: str | None = None, *, scanner=None,
                 client_factory=None, clock=time.monotonic):
        self.serial_number = selected_serial(serial_number)
        self._scanner = scanner
        self._client_factory = client_factory
        self._clock = clock
        self._runner = asyncio.Runner()
        self._client = None
        self._device = None
        self._pending = bytearray()
        self._received = None
        self._overflow = False
        self._needs_boundary = True
        self._failures = 0
        self._retry_at = 0.0
        self._failed = False
        self._generation = 0
        self._closed = False

    def _load_backend(self):
        if self._scanner is None or self._client_factory is None:
            try:
                from bleak import BleakClient
            except ImportError:
                raise RuntimeError("Install requirements.txt to enable Tab5 Bluetooth metadata") from None
            self._scanner = self._scanner or MetadataScanner
            if self._client_factory is None:
                self._client_factory = (partial(BleakClient, backend=_corebluetooth_client_backend())
                                        if sys.platform == "darwin" else BleakClient)

    def _notify(self, _characteristic, data):
        if len(self._pending) + len(data) > MAX_REPLY:
            self._pending.clear()
            self._overflow = True
        elif not self._overflow:
            self._pending.extend(data)
        if self._received is not None:
            self._received.set()

    def _disconnected(self, _client):
        self.invalidate()

    def _notify_session(self, generation, characteristic, data):
        if generation == self._generation and not self._failed:
            self._notify(characteristic, data)

    def _disconnect_session(self, generation, client):
        if generation == self._generation:
            self._disconnected(client)

    def invalidate(self):
        """Quarantine a failed GATT client without reconnecting or replaying.

        CoreBluetooth's is_connected can remain true because the native HID
        client still owns the physical link. It is not evidence that this
        client's pending ATT requests or notification subscription survived.
        Defer cleanup to connect(), outside the USB transaction lock.
        """
        if not self._failed:
            self._failed = True
            self._generation += 1
            self._failures += 1
            self._retry_at = self._clock() + min(30.0, 2.0 ** min(self._failures, 5))
        self._pending.clear()
        self._needs_boundary = True
        if self._received is not None:
            self._received.set()

    async def _connect(self):
        # Let any pending CoreBluetooth disconnect callbacks run first.
        await asyncio.sleep(0)
        if self._failed:
            # Cancel only our CoreBluetooth connection request. The system HID
            # connection has its own owner; never disable Bluetooth or unpair.
            await self._disconnect()
            self._device = None
            self._failed = False
        if self._client is not None and self._client.is_connected:
            return
        if self._clock() < self._retry_at:
            raise RuntimeError("Tab5 BLE reconnect backoff is active")
        self._load_backend()
        self._pending.clear()
        self._overflow = False
        self._received = asyncio.Event()
        stage = "discovery"
        try:
            if self._device is None:
                discovered = await self._scanner.discover(
                    timeout=4.0, return_adv=True, service_uuids=[SERVICE_UUID])
                self._device = choose_candidate(discovered, self.serial_number)
            stage = "connect"
            self._generation += 1
            generation = self._generation
            self._client = self._client_factory(
                self._device, disconnected_callback=partial(self._disconnect_session, generation),
                services=[SERVICE_UUID], timeout=12.0,
            )
            await asyncio.wait_for(self._client.connect(), 15.0)
            # With the real firmware, first access can wait for a human to
            # unlock macOS and enter the displayed PIN. Keep link establishment
            # bounded separately; the USB transaction lock is not held while
            # authentication waits.
            stage = "identity-read"
            raw = await asyncio.wait_for(self._client.read_gatt_char(IDENTITY_UUID), AUTH_TIMEOUT_SECONDS)
            stage = "identity-check"
            identity = decode_identity(raw, self.serial_number)
            stage = "notify"
            await asyncio.wait_for(self._client.start_notify(
                TX_UUID, partial(self._notify_session, generation)), 5.0)
            if self._failed or generation != self._generation or not self._client.is_connected:
                raise RuntimeError("Tab5 BLE connection changed during setup")
            self.serial_number = identity
            self._needs_boundary = True
            self._failures = 0
            self._retry_at = 0.0
        except Exception as error:
            self._failures += 1
            self._retry_at = self._clock() + min(30.0, 2.0 ** min(self._failures, 5))
            self._device = None
            await self._disconnect()
            raise RuntimeError(
                f"Tab5 BLE metadata connection or authentication failed ({stage}; {_failure_detail(error)})"
            ) from None

    def connect(self):
        if self._closed:
            raise RuntimeError("Tab5 BLE stream is closed")
        self._runner.run(self._connect())

    async def _disconnect(self):
        self._generation += 1
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 3.0)
            except Exception:
                pass
        self._pending.clear()
        self._needs_boundary = True
        self._overflow = False

    async def _write(self, command: bytes):
        if self._failed or self._client is None or not self._client.is_connected:
            raise RuntimeError("Tab5 BLE metadata disconnected")
        if self._needs_boundary:
            # A timed-out ATT write may have delivered all JSON except its
            # newline. CAN aborts the parser without executing that old state.
            await self._client.write_gatt_char(RX_UUID, b"\x18", response=True)
            self._needs_boundary = False
            self._pending.clear()
            self._overflow = False
        mtu = self._client.mtu_size
        chunk_size = min(512, max(20, mtu - 3)) if type(mtu) is int else 20
        for offset in range(0, len(command), chunk_size):
            await self._client.write_gatt_char(RX_UUID, command[offset:offset + chunk_size], response=True)

    def write(self, command: bytes):
        if self._closed:
            raise RuntimeError("Tab5 BLE stream is closed")
        if (not isinstance(command, bytes) or len(command) > MAX_LINE
                or not command.endswith(b"\n") or b"\n" in command[:-1]
                or not command.startswith((b"TITLES ", b"HOST_CONFIG "))):
            raise ValueError("BLE metadata accepts one bounded TITLES or HOST_CONFIG line")
        try:
            self._runner.run(asyncio.wait_for(self._write(command), 4.0))
        except Exception as error:
            self.invalidate()
            raise RuntimeError(
                f"Tab5 BLE metadata write did not complete (write; {_failure_detail(error)})"
            ) from None
        return len(command)

    def flush(self):
        # Every ATT fragment already used write-with-response.
        pass

    async def _read(self, delimiter: bytes, limit: int):
        deadline = self._runner.get_loop().time() + 0.2
        while True:
            if self._failed or self._client is None or not self._client.is_connected:
                raise RuntimeError("Tab5 BLE metadata disconnected")
            if self._overflow:
                self._needs_boundary = True
                raise RuntimeError("Tab5 BLE reply exceeded its size limit")
            index = self._pending.find(delimiter)
            if index >= 0 or len(self._pending) >= limit:
                size = min(index + len(delimiter), limit) if index >= 0 else limit
                result = bytes(self._pending[:size])
                del self._pending[:size]
                return result
            remaining = deadline - self._runner.get_loop().time()
            if remaining <= 0:
                result = bytes(self._pending)
                self._pending.clear()
                return result
            self._received.clear()
            try:
                await asyncio.wait_for(self._received.wait(), remaining)
            except TimeoutError:
                continue

    def read_until(self, delimiter=b"\n", limit=MAX_REPLY):
        if self._closed:
            raise RuntimeError("Tab5 BLE stream is closed")
        if delimiter != b"\n" or not 0 < limit <= MAX_REPLY:
            raise ValueError("Invalid BLE metadata read boundary")
        try:
            return self._runner.run(self._read(delimiter, limit))
        except Exception as error:
            self.invalidate()
            raise RuntimeError(
                f"Tab5 BLE metadata read did not complete (read; {_failure_detail(error)})"
            ) from None

    def close(self):
        if not self._closed:
            try:
                self._runner.run(self._disconnect())
            finally:
                self._runner.close()
                self._closed = True
