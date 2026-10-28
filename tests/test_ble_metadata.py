"""Synthetic BLE/USB metadata contracts; never access Bluetooth or USB."""

from contextlib import contextmanager, nullcontext
import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chat_sync
import desktop_sync
import tab5_ble as ble
import tab5_metadata as metadata
import tab5_transport as usb
import title_sync
import title_sync_service


CHIP = "TAB5-0123456789AB"
OTHER_CHIP = "TAB5-AABBCCDDEEFF"


def identity(serial_number=CHIP, **changes):
    return json.dumps({"v": 1, "serial": serial_number, "maxLine": 6144,
                       "firmware": "synthetic-build", **changes}).encode()


def advertised(address="synthetic-peripheral", *, hint=None, service_data=None, services=None):
    return (SimpleNamespace(address=address, name="Untrusted arbitrary name"),
            SimpleNamespace(service_uuids=services or [ble.SERVICE_UUID],
                            service_data=service_data or {},
                            manufacturer_data={} if hint is None else {0x303A: hint}))


class IdentityTests(unittest.TestCase):
    def test_already_connected_hid_is_retrievable_without_an_advertisement(self):
        connected = advertised("known-connected")
        scanner = FakeScanner({})
        with mock.patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakScanner=scanner)}), \
                mock.patch.object(ble, "_connected_peripherals", new=mock.AsyncMock(return_value={"KNOWN-CONNECTED": connected})):
            result = asyncio.run(ble.MetadataScanner.discover(timeout=4, return_adv=True,
                                                              service_uuids=[ble.SERVICE_UUID]))
        self.assertIs(ble.choose_candidate(result), connected[0])

    def test_connected_and_advertising_same_device_are_one_candidate(self):
        connected = advertised("same")
        advertisement = advertised("same", hint=bytes.fromhex("0123456789AB"))
        scanner = FakeScanner({"same": advertisement})
        with mock.patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakScanner=scanner)}), \
                mock.patch.object(ble, "_connected_peripherals", new=mock.AsyncMock(return_value={"SAME": connected})):
            result = asyncio.run(ble.MetadataScanner.discover(timeout=4))
        self.assertEqual(len(result), 1)
        self.assertIs(ble.choose_candidate(result, CHIP), advertisement[0])

    def test_failed_scan_retains_verified_service_candidate_without_ignoring_ambiguity(self):
        connected = advertised("known-connected")
        scanner = SimpleNamespace(discover=mock.AsyncMock(side_effect=OSError("scan unavailable")))
        with mock.patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakScanner=scanner)}), \
                mock.patch.object(ble, "_connected_peripherals", new=mock.AsyncMock(return_value={"KNOWN-CONNECTED": connected})):
            result = asyncio.run(ble.MetadataScanner.discover(timeout=4))
        self.assertIs(ble.choose_candidate(result), connected[0])
        with self.assertRaises(ValueError):
            ble.decode_identity(identity(OTHER_CHIP), CHIP)

    def test_versioned_chip_identity_is_required_and_firmware_version_is_diagnostic(self):
        self.assertEqual(ble.decode_identity(identity(), CHIP.lower()), CHIP)
        self.assertEqual(ble.decode_identity(identity(firmware="a later version")), CHIP)
        for data in (identity(OTHER_CHIP), identity("C6-AABBCCDDEEFF"),
                     identity(""), identity(v=True), identity(v=2),
                     identity(maxLine=8192), identity(maxLine=True), b"null", b"bad", b"a" * 513):
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                ble.decode_identity(data, CHIP)

    def test_no_name_or_address_selection_and_multiple_unknown_candidates_fail_closed(self):
        first = advertised()
        self.assertIs(ble.choose_candidate({"a": first}), first[0])
        with self.assertRaises(RuntimeError):
            ble.choose_candidate({"a": first, "b": advertised("other")})
        with self.assertRaises(RuntimeError):
            ble.choose_candidate({"a": advertised(services=["1812"])})

    def test_manufacturer_serial_hint_only_narrows_to_one_candidate(self):
        wanted = advertised(hint=bytes.fromhex("0123456789AB"))
        other = advertised("other", hint=bytes.fromhex("AABBCCDDEEFF"))
        self.assertIs(ble.choose_candidate({"a": wanted, "b": other}, CHIP), wanted[0])
        # Hints are not trusted: the encrypted Identity must still match.
        with self.assertRaises(ValueError):
            ble.decode_identity(identity(OTHER_CHIP), CHIP)
        with self.assertRaises(RuntimeError):
            ble.choose_candidate({"b": other}, CHIP)

    def test_optional_service_data_is_supported_but_not_a_proof_of_identity(self):
        wanted = advertised(service_data={ble.SERVICE_UUID: CHIP.encode()})
        self.assertIs(ble.choose_candidate({"a": wanted}, CHIP), wanted[0])
        with self.assertRaises(RuntimeError):
            ble.choose_candidate({"a": advertised(service_data={ble.SERVICE_UUID: b"bad"})}, CHIP)


class CoreBluetoothAdapterTests(unittest.TestCase):
    def test_identity_timeout_reaches_the_inner_delegate_without_changing_other_reads(self):
        class BaseClient:
            async def read_gatt_char(self, characteristic, **options):
                self.parent_read = (characteristic, options)
                return bytearray(b"ordinary read")
        module = SimpleNamespace(BleakClientCoreBluetooth=BaseClient)
        with mock.patch.dict(sys.modules, {"bleak.backends.corebluetooth.client": module}):
            backend = ble._corebluetooth_client_backend()()
        backend._delegate = SimpleNamespace(read_characteristic=mock.AsyncMock(return_value=identity()))
        characteristic = SimpleNamespace(uuid=ble.IDENTITY_UUID.upper(), obj=object())
        self.assertEqual(asyncio.run(backend.read_gatt_char(characteristic)), bytearray(identity()))
        backend._delegate.read_characteristic.assert_awaited_once_with(
            characteristic.obj, use_cached=False, timeout=60.0)
        other = SimpleNamespace(uuid=ble.TX_UUID, obj=object())
        self.assertEqual(asyncio.run(backend.read_gatt_char(other)), bytearray(b"ordinary read"))
        self.assertEqual(backend.parent_read, (other, {"use_cached": False}))
        self.assertEqual(backend._delegate.read_characteristic.await_count, 1)

    def test_default_macos_client_uses_public_backend_override(self):
        factory = mock.Mock()
        marker = object()
        with mock.patch.object(ble.sys, "platform", "darwin"), \
                mock.patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakClient=factory)}), \
                mock.patch.object(ble, "_corebluetooth_client_backend", return_value=marker):
            stream = ble.BleMetadataStream(CHIP, scanner=FakeScanner())
            try:
                stream._load_backend()
                stream._client_factory("synthetic-device", timeout=12)
            finally:
                stream.close()
        factory.assert_called_once_with("synthetic-device", timeout=12, backend=marker)

    def test_error_diagnostics_keep_only_bounded_codes_and_categories(self):
        private = "secret PIN 001234 peer synthetic-private-id"
        cases = ((TimeoutError(private), "timeout"),
                 (ValueError(private), "invalid-data"),
                 (OSError(private), "os-error"),
                 (RuntimeError(private), "backend-error"),
                 (RuntimeError(f"Error Domain=CBATTErrorDomain Code=5 {private}"), "CBATTErrorDomain:5"),
                 (RuntimeError(f"Error Domain=CBErrorDomain Code=7 {private}"), "CBErrorDomain:7"),
                 (RuntimeError(f"Error Domain=PRIVATE Code=5 {private}"), "backend-error"))
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(ble._failure_detail(error), expected)


class FakeClient:
    def __init__(self, device, *, disconnected_callback, services, timeout,
                 raw_identity=None, fail_connect=False):
        self.device = device
        self.callback = disconnected_callback
        self.services_filter = services
        self.timeout = timeout
        self.raw_identity = raw_identity if raw_identity is not None else identity()
        self.fail_connect = fail_connect
        self.is_connected = False
        self.mtu_size = 23
        self.notifier = None
        self.disconnects = 0
        self.reads = []
        self.fragments = []
        self.lines = []
        self.pending = bytearray()
        self.fail_fragment = None

    async def connect(self):
        if self.fail_connect:
            raise OSError("Synthetic backend failure with private details")
        self.is_connected = True

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        return self.raw_identity

    async def start_notify(self, uuid, callback):
        assert uuid == ble.TX_UUID
        self.notifier = callback

    async def write_gatt_char(self, uuid, value, *, response):
        assert uuid == ble.RX_UUID and response is True
        self.fragments.append(bytes(value))
        if value == b"\x18":
            self.pending.clear()
        else:
            self.pending.extend(value)
            if self.fail_fragment == len(self.fragments):
                raise OSError("Synthetic partial ATT failure")
            if b"\n" in self.pending:
                line = bytes(self.pending)
                self.pending.clear()
                self.lines.append(line)
                name, raw = line.split(b" ", 1)
                sequence = json.loads(raw)["seq"]
                self.notifier(None, bytearray(name + b"_"))
                self.notifier(None, bytearray(f"OK {sequence}\n".encode()))

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False
        self.callback(self)


class FakeScanner:
    def __init__(self, discovered=None):
        self.calls = []
        self.discovered = discovered if discovered is not None else {"a": advertised()}

    async def discover(self, **options):
        self.calls.append(options)
        return self.discovered


class BleStreamTests(unittest.TestCase):
    def setUp(self):
        self.scanner = FakeScanner()
        self.clients = []
        self.options = {}
        self.now = 0
        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs, **self.options)
            self.clients.append(client)
            return client
        self.stream = ble.BleMetadataStream(CHIP, scanner=self.scanner,
                                             client_factory=factory, clock=lambda: self.now)

    def tearDown(self):
        self.stream.close()

    def test_identity_read_precedes_metadata_and_persistent_connect_reuses_one_client(self):
        self.stream.connect()
        self.stream.connect()
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0].reads, [ble.IDENTITY_UUID])
        self.assertEqual(self.clients[0].services_filter, [ble.SERVICE_UUID])
        self.assertEqual(self.clients[0].disconnects, 0)
        self.assertEqual(self.clients[0].fragments, [])

    def test_manual_pin_budget_does_not_extend_link_establishment_timeout(self):
        original = asyncio.wait_for
        with mock.patch.object(ble.asyncio, "wait_for", wraps=original) as waiting:
            self.stream.connect()
        self.assertEqual([call.args[1] for call in waiting.call_args_list], [15.0, 60.0, 5.0])
        self.assertEqual(self.clients[0].fragments, [])

    def test_unicode_titles_are_losslessly_fragmented_and_notifications_reassemble(self):
        self.stream.connect()
        title = "Synthetic 中文 title " * 5
        command = ("TITLES " + json.dumps({"seq": 9, "title": title}, ensure_ascii=False) + "\n").encode()
        self.assertEqual(self.stream.write(command), len(command))
        self.assertEqual(self.clients[0].fragments[0], b"\x18")
        self.assertTrue(all(len(part) <= 20 for part in self.clients[0].fragments))
        self.assertEqual(self.clients[0].lines, [command])
        self.assertEqual(self.stream.read_until(), b"TITLES_OK 9\n")

    def test_negotiated_mtu_and_write_response_are_used(self):
        self.stream.connect()
        self.clients[0].mtu_size = 185
        command = b'TITLES {"seq":10,"title":"' + b"a" * 400 + b'"}\n'
        self.stream.write(command)
        self.assertEqual([len(chunk) for chunk in self.clients[0].fragments[1:-1]], [182, 182])
        self.assertEqual(self.stream.read_until(), b"TITLES_OK 10\n")

    def test_identity_mismatch_never_sends_metadata(self):
        self.options["raw_identity"] = identity(OTHER_CHIP)
        with self.assertRaises(RuntimeError):
            self.stream.connect()
        self.assertEqual(self.clients[0].fragments, [])
        self.assertEqual(self.clients[0].disconnects, 1)
        self.assertEqual(self.stream.serial_number, CHIP)

    def test_identity_timeout_reports_stage_without_private_backend_text(self):
        with mock.patch.object(FakeClient, "read_gatt_char", new=mock.AsyncMock(
                side_effect=TimeoutError("private peer and PIN"))):
            with self.assertRaisesRegex(RuntimeError, r"\(identity-read; timeout\)$") as caught:
                self.stream.connect()
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.clients[0].disconnects, 1)
        self.assertEqual(self.clients[0].fragments, [])

    def test_disconnect_during_subscription_cannot_be_reported_as_ready(self):
        async def disconnect_while_subscribing(client, _uuid, _callback):
            client.callback(client)
        with mock.patch.object(FakeClient, "start_notify", new=disconnect_while_subscribing):
            with self.assertRaisesRegex(RuntimeError, r"\(notify; backend-error\)$"):
                self.stream.connect()
        self.assertEqual(self.clients[0].disconnects, 1)
        self.assertEqual(self.clients[0].fragments, [])

    def test_disconnected_read_fails_and_reconnect_never_replays_old_packets(self):
        self.stream.connect()
        self.stream.write(b'TITLES {"seq":1}\n')
        first = self.clients[0]
        first.is_connected = False
        first.callback(first)
        with self.assertRaises(RuntimeError):
            self.stream.read_until()
        self.now = 2
        self.stream.connect()
        second = self.clients[1]
        self.assertEqual(second.fragments, [])
        self.assertEqual(second.reads, [ble.IDENTITY_UUID])
        self.stream.write(b'TITLES {"seq":2}\n')
        self.assertEqual(second.lines, [b'TITLES {"seq":2}\n'])
        self.assertEqual(self.stream.read_until(), b"TITLES_OK 2\n")

    def test_failed_att_write_aborts_incomplete_line_before_fresh_packet(self):
        self.stream.connect()
        client = self.clients[0]
        client.fail_fragment = 2
        with self.assertRaises(RuntimeError):
            self.stream.write(b'HOST_CONFIG {"seq":1}\n')
        self.assertTrue(client.pending)
        self.assertTrue(client.is_connected)  # Native HID may retain the link.
        with self.assertRaises(RuntimeError):
            self.stream.write(b'HOST_CONFIG {"seq":99}\n')
        self.assertEqual(len(client.fragments), 2)
        self.now = 2
        self.stream.connect()
        replacement = self.clients[1]
        # Same physical peer may retain a partial line across app reconnection.
        replacement.pending.extend(client.pending)
        self.stream.write(b'HOST_CONFIG {"seq":2}\n')
        self.assertEqual(client.disconnects, 1)
        self.assertEqual(client.lines, [])
        self.assertEqual(replacement.reads, [ble.IDENTITY_UUID])
        self.assertEqual(replacement.fragments[0], b"\x18")
        self.assertEqual(replacement.lines, [b'HOST_CONFIG {"seq":2}\n'])
        self.assertEqual(self.stream.read_until(), b"HOST_CONFIG_OK 2\n")

    def test_read_failure_reconnects_even_while_physical_link_remains_connected(self):
        self.stream.connect()
        previous = self.clients[0]
        previous.notifier(None, b"x" * 8193)
        with self.assertRaisesRegex(RuntimeError, "read did not complete"):
            self.stream.read_until()
        self.assertTrue(previous.is_connected)
        self.now = 2
        self.stream.connect()
        self.assertEqual(previous.disconnects, 1)
        self.stream.write(b'TITLES {"seq":3}\n')
        self.assertEqual(self.stream.read_until(), b"TITLES_OK 3\n")

    def test_old_client_callbacks_cannot_clear_or_poison_the_reconnected_stream(self):
        self.stream.connect()
        previous = self.clients[0]
        self.stream.invalidate()
        self.now = 2
        self.stream.connect()
        self.stream.write(b'TITLES {"seq":4}\n')
        previous.notifier(None, b"x" * 8193)
        previous.callback(previous)
        self.assertEqual(self.stream.read_until(), b"TITLES_OK 4\n")
        self.assertFalse(self.stream._failed)

    def test_short_read_timeout_is_idle_not_a_failed_connection(self):
        self.stream.connect()
        self.assertEqual(self.stream.read_until(), b"")
        self.assertFalse(self.stream._failed)
        self.stream.connect()
        self.assertEqual(len(self.clients), 1)

    def test_connect_failure_backoff_is_bounded_and_does_not_expose_backend_details(self):
        self.options["fail_connect"] = True
        for instant, expected_clients in ((0, 1), (1, 1), (2, 2), (5, 2), (6, 3)):
            self.now = instant
            with self.subTest(instant=instant), self.assertRaises(RuntimeError) as caught:
                self.stream.connect()
            self.assertNotIn("private details", str(caught.exception))
            self.assertEqual(len(self.clients), expected_clients)
        self.options.clear()
        self.now = 60
        self.stream.connect()
        self.assertTrue(self.clients[-1].is_connected)

    def test_overflow_and_unsupported_commands_are_rejected_without_payload_logs(self):
        self.stream.connect()
        for command in (b"SCREEN\n", b'TITLES {}\nHOST_CONFIG {}\n', b'TITLES {}', b'TITLES ' + b'a' * 6144 + b'\n'):
            with self.subTest(command=command[:20]), self.assertRaises(ValueError):
                self.stream.write(command)
        self.assertEqual(self.clients[0].fragments, [])
        self.stream._notify(None, b"x" * 8193)
        with self.assertRaises(RuntimeError):
            self.stream.read_until()


class FakePersistentStream:
    def __init__(self, serial_number):
        self.serial_number = serial_number or CHIP
        self.connects = 0
        self.closed = False
        self.on_connect = None
        self.invalidations = 0

    def connect(self):
        self.connects += 1
        if self.on_connect:
            self.on_connect()

    def close(self):
        self.closed = True

    def invalidate(self):
        self.invalidations += 1


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tab5-ble-tests-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.ports = []
        mock.patch.dict(usb.os.environ, {"TAB5_SERIAL_NUMBER": ""}).start()
        mock.patch.object(usb.list_ports, "comports", side_effect=lambda: self.ports).start()
        mock.patch.object(metadata, "BLE_OWNER_PATH", Path(self.temp.name) / "owner.lock").start()
        self.serial = object()
        self.open_usb = mock.patch.object(usb, "open_serial", side_effect=lambda *args, **kwargs: nullcontext(self.serial)).start()
        mock.patch.object(usb, "serial_lock", side_effect=lambda *args, **kwargs: nullcontext()).start()
        self.factory = mock.Mock(side_effect=FakePersistentStream)
        self.transport = metadata.MetadataTransport(ble_factory=self.factory)
        self.addCleanup(self.transport.close)

    def port(self, chip=CHIP):
        return SimpleNamespace(device="/synthetic/tab5", vid=0x303A, pid=0x8360, serial_number=chip)

    def use(self, transport=None):
        with (transport or self.transport).connection() as connection:
            return connection

    def test_usb_preferred_pins_p4_identity_without_loading_ble(self):
        self.ports = [self.port()]
        self.assertIs(self.use(), self.serial)
        self.assertEqual(self.transport.serial_number, CHIP)
        self.factory.assert_not_called()
        self.open_usb.assert_called_once_with("/synthetic/tab5", 0.5, serial_number=CHIP)

    def test_usb_busy_or_failed_does_not_fall_back_to_ble(self):
        self.ports = [self.port()]
        for error in (usb.SerialBusyError("busy"), OSError("gone")):
            self.open_usb.side_effect = error
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.use()
        self.factory.assert_not_called()

    def test_usb_removed_uses_same_chip_and_replug_keeps_gatt_alive(self):
        self.ports = [self.port()]
        self.use()
        self.ports = []
        stream = self.use()
        self.assertEqual(stream.serial_number, CHIP)
        self.assertIs(self.use(), stream)
        self.factory.assert_called_once_with(CHIP)
        self.ports = [self.port()]
        self.assertIs(self.use(), self.serial)
        self.assertFalse(stream.closed)
        self.assertEqual(self.transport.active, "usb")

    def test_selected_identity_supports_wireless_cold_start_without_usb(self):
        transport = metadata.MetadataTransport("auto", CHIP, ble_factory=self.factory)
        self.addCleanup(transport.close)
        self.assertEqual(self.use(transport).serial_number, CHIP)
        self.open_usb.assert_not_called()
        self.factory.assert_called_once_with(CHIP)

    def test_usb_arrives_during_ble_connect_wins_before_any_metadata(self):
        stream = FakePersistentStream(CHIP)
        stream.on_connect = lambda: self.ports.append(self.port())
        self.factory.side_effect = lambda _: stream
        self.assertIs(self.use(), self.serial)
        self.assertFalse(stream.closed)
        self.assertEqual(self.transport.active, "usb")

    def test_second_owner_cannot_connect_or_disconnect_first_owner(self):
        stream = self.use()
        second_factory = mock.Mock(side_effect=FakePersistentStream)
        second = metadata.MetadataTransport(ble_factory=second_factory)
        with self.assertRaises(usb.SerialBusyError):
            self.use(second)
        second.close()
        second_factory.assert_not_called()
        self.assertFalse(stream.closed)
        self.transport.close()
        self.addCleanup(second.close)
        self.assertIsInstance(self.use(second), FakePersistentStream)

    def test_multiple_usb_or_explicit_missing_port_never_fall_back(self):
        self.ports = [self.port(), self.port(OTHER_CHIP)]
        with self.assertRaises(RuntimeError):
            self.use()
        self.ports = []
        with self.assertRaises(RuntimeError):
            with self.transport.connection("/synthetic/missing"):
                self.fail("Unexpected connection")
        self.factory.assert_not_called()

    def test_forced_usb_does_not_import_or_connect_bluetooth(self):
        with metadata.MetadataTransport("usb", CHIP, ble_factory=self.factory) as transport:
            with self.assertRaises(RuntimeError):
                self.use(transport)
        self.factory.assert_not_called()

    def test_forced_ble_rejects_explicit_usb_port(self):
        with metadata.MetadataTransport("ble", CHIP, ble_factory=self.factory) as transport:
            with self.assertRaises(ValueError):
                with transport.connection("/synthetic/tab5"):
                    self.fail("Unexpected connection")
        self.factory.assert_not_called()

    def test_application_ack_timeout_invalidates_ble_but_rejection_does_not(self):
        stream = self.use()
        with self.assertRaises(TimeoutError):
            with self.transport.connection():
                raise TimeoutError("Synthetic missing ACK")
        self.assertEqual(stream.invalidations, 1)
        with self.assertRaises(RuntimeError):
            with self.transport.connection():
                raise RuntimeError("Synthetic rejected metadata")
        self.assertEqual(stream.invalidations, 1)
        self.assertFalse(stream.closed)


class FreshnessTests(unittest.TestCase):
    def test_default_once_uses_auto_and_reports_actual_acknowledged_transport(self):
        replies = []
        connection = mock.Mock()
        def write(command):
            raw = json.loads(command.split(b" ", 1)[1])
            replies.append(command.split(b" ", 1)[0] + f'_OK {raw["seq"]}\n'.encode())
            return len(command)
        connection.write.side_effect = write
        connection.read_until.side_effect = lambda *_: replies.pop(0)
        owner = mock.MagicMock()
        owner.__enter__.return_value = owner
        owner.connection.side_effect = lambda *_: nullcontext(connection)
        owner.active = "ble"
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["title_sync.py", "--once"]), \
                mock.patch.object(title_sync, "MetadataTransport", return_value=owner) as factory, \
                mock.patch.object(title_sync, "build_snapshot", return_value=title_sync.unavailable("Fixture")), \
                mock.patch.object(desktop_sync, "build_snapshot", return_value=desktop_sync.unavailable()), \
                mock.patch.object(chat_sync, "build_snapshot", return_value=chat_sync.ChatSnapshot()), \
                mock.patch("sys.stdout", output):
            self.assertEqual(title_sync.main(), 0)
        factory.assert_called_once_with("auto", None)
        self.assertEqual(json.loads(output.getvalue())["delivery"], {"status": "acknowledged", "transport": "ble"})
        self.assertEqual(connection.write.call_count, 2)

    def test_partial_transport_write_does_not_append_another_packet(self):
        connection = mock.Mock()
        connection.write.return_value = 3
        with mock.patch.object(title_sync, "open_serial", return_value=nullcontext(connection)):
            with self.assertRaises(OSError):
                title_sync.send_snapshots(title_sync.unavailable("Fixture"), desktop_sync.unavailable(), 7)
        self.assertEqual(connection.write.call_count, 1)
        connection.read_until.assert_not_called()

    def test_snapshots_are_rebuilt_after_connect_and_again_after_title_transfer(self):
        titles = title_sync.unavailable("Synthetic fixture")
        old = desktop_sync.DesktopSnapshot("local-config", "fast", "reasoning", "realtime", ("none",) * 7,
                                          chat=chat_sync.ChatSnapshot("desktop-ipc", "explicit-target",
                                            "00000000-0000-0000-0000-000000000001", "Synthetic", "fast", "plan", "full-access"))
        fresh = desktop_sync.unavailable("Synthetic expired cache")
        trace = []
        class Connection:
            pending = b""
            def write(self, command):
                trace.append(("write", command))
                self.pending = command.split(b" ", 1)[0] + b"_OK 7\n"
                return len(command)
            def flush(self):
                pass
            def read_until(self, *_):
                result, self.pending = self.pending, b""
                return result
        class Transport:
            @contextmanager
            def connection(self, *_):
                trace.append(("connected", None))
                yield Connection()
        def refresh():
            trace.append(("refresh", None))
            return titles, fresh
        title_sync.send_snapshots(titles, old, 7, transport=Transport(), refresh=refresh)
        self.assertEqual([event for event, _ in trace], ["connected", "refresh", "write", "refresh", "write"])
        sent = json.loads(trace[-1][1].split(b" ", 1)[1])
        self.assertEqual(sent["source"], "unavailable")
        self.assertNotIn("Synthetic", trace[-1][1].decode())
        self.assertNotIn("full-access", trace[-1][1].decode())


class ServiceTests(unittest.TestCase):
    def test_service_persists_exact_serial_and_uses_only_one_hardware_owner(self):
        with mock.patch.dict(usb.os.environ, {"TAB5_SERIAL_NUMBER": ""}):
            plist = title_sync_service.service_plist("auto", CHIP.lower())
        self.assertEqual(plist["ProgramArguments"][-4:], ["--transport", "auto", "--device-serial", CHIP])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["EnvironmentVariables"], {"PYTHONUNBUFFERED": "1"})
        with self.assertRaises(ValueError):
            title_sync_service.service_plist("auto", "C6-not-the-P4")


if __name__ == "__main__":
    unittest.main()
