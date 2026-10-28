"""Source flash identity, reconnect and error tests; never enumerate or open USB."""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import flash
from test_release_bundle import firmware_fixture, port

VERSION = '0.11.0'
FIRMWARE = 'tab5-micro-' + VERSION
TARGET_SERIAL = 'TAB5-0123456789AB'
OTHER_SERIAL = 'TAB5-AABBCCDDEEFF'
MAC_OUTPUT = 'Chip is ESP32-P4\nMAC:                 01:23:45:67:89:ab\n'


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SourceFlashTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='tab5-source-flash-test-'))
        self.image = Path(self.temporary) / 'firmware.factory.bin'
        self.image.write_bytes(firmware_fixture())
        self.args = SimpleNamespace(port='selected-rom', font_image=None)
        self.clock = Clock()
        self.stack.enter_context(patch.object(flash.time, 'monotonic', self.clock.monotonic))
        self.stack.enter_context(patch.object(flash.time, 'sleep', self.clock.sleep))
        self.provider = self.stack.enter_context(patch.object(flash.list_ports, 'comports'))
        self.provider.return_value = [port('selected-rom', flash.ROM_PID)]
        # Any accidental device open is a test failure, including an unrelated S3.
        self.serial = self.stack.enter_context(patch.object(flash.serial, 'Serial',
                                                           side_effect=AssertionError('Unexpected USB open')))
        self.runner = self.stack.enter_context(patch.object(flash, 'run'))
        self.runner.return_value = SimpleNamespace(stdout=MAC_OUTPUT)
        self.status = self.stack.enter_context(patch.object(flash, 'read_firmware_status', return_value=FIRMWARE))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_first_rom_flash_verifies_only_its_mac_and_version_after_port_rename(self):
        other = port('unrelated-existing-tab5', serial_number=OTHER_SERIAL)
        expected = port('renamed-application')
        self.provider.side_effect = [[port('selected-rom', flash.ROM_PID), other], [other], [other, expected]]
        self.args.font_image = Path('title-font.bin')
        self.assertEqual(flash.flash(self.args, self.image), 'renamed-application')
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0][-1], 'read-mac')
        self.assertIn('--no-stub', commands[0])
        self.assertTrue(self.runner.call_args_list[0].kwargs['capture_output'])
        self.assertIn('write-mem', commands[1])
        self.assertIn('write-flash', commands[2])
        self.assertIn('0x510000', commands[2])
        self.assertIn('watchdog-reset', commands[3])
        for command in commands:
            self.assertEqual(command[command.index('--chip') + 1], 'esp32p4')
            self.assertEqual(command[command.index('--port') + 1], 'selected-rom')
        self.status.assert_called_once_with('renamed-application', 3)
        self.assertIn('verified', self.output.getvalue())
        self.assertNotIn('unrelated-existing-tab5', self.output.getvalue())

    def test_unrelated_working_device_cannot_make_flash_report_success(self):
        self.provider.side_effect = lambda: ([port('selected-rom', flash.ROM_PID)] if self.runner.call_count == 0
                                            else [port('other', serial_number=OTHER_SERIAL)])
        with self.assertRaisesRegex(RuntimeError, 'same Tab5'):
            flash.flash(self.args, self.image)
        self.status.assert_not_called()
        self.assertNotIn('verified', self.output.getvalue())

    def test_matching_identity_with_wrong_version_is_not_success(self):
        self.provider.side_effect = [[port('selected-rom', flash.ROM_PID)],
                                     [port('other', serial_number=OTHER_SERIAL), port('target-app')]]
        self.status.return_value = FIRMWARE + '-rc.1'
        with self.assertRaisesRegex(RuntimeError, 'expected firmware version'):
            flash.flash(self.args, self.image)
        self.status.assert_called_once_with('target-app', 3)
        self.assertNotIn('verified', self.output.getvalue())

    def test_p4_guard_failure_prevents_all_writes(self):
        self.runner.side_effect = subprocess.CalledProcessError(2, ['esptool', '--chip', 'esp32p4', 'read-mac'])
        with self.assertRaises(subprocess.CalledProcessError):
            flash.flash(self.args, self.image)
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.runner.call_args.args[0][-1], 'read-mac')
        self.status.assert_not_called()

    def test_missing_or_ambiguous_mac_prevents_all_writes(self):
        for output in ('MAC: unknown\n', MAC_OUTPUT + 'MAC: aa:bb:cc:dd:ee:ff\n'):
            self.runner.reset_mock()
            self.runner.return_value = SimpleNamespace(stdout=output)
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, 'base MAC'):
                flash.flash(self.args, self.image)
            self.assertEqual(self.runner.call_count, 1)
            self.status.assert_not_called()

    def test_application_to_rom_must_preserve_chip_identity(self):
        self.args.port = 'selected-app'
        self.provider.return_value = [port('selected-app')]
        self.runner.return_value = SimpleNamespace(stdout='MAC: aa:bb:cc:dd:ee:ff\n')
        with patch.object(flash, 'enter_rom', return_value='new-rom') as enter:
            with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                flash.flash(self.args, self.image)
        self.assertEqual(enter.call_args.args[0].device, 'selected-app')
        self.assertEqual(self.runner.call_count, 1)
        self.status.assert_not_called()

    def test_corrupt_factory_fails_before_port_selection(self):
        self.image.write_bytes(b'not firmware')
        with self.assertRaises(RuntimeError):
            flash.flash(self.args, self.image)
        self.provider.assert_not_called()
        self.runner.assert_not_called()

    def test_failed_flash_does_not_reset_or_claim_reconnect(self):
        self.runner.side_effect = [SimpleNamespace(stdout=MAC_OUTPUT), None,
                                   subprocess.CalledProcessError(2, ['esptool', 'write-flash'])]
        with self.assertRaises(subprocess.CalledProcessError):
            flash.flash(self.args, self.image)
        self.assertEqual(self.runner.call_count, 3)
        self.status.assert_not_called()
        self.assertNotIn('verified', self.output.getvalue())

    def test_reconnect_retries_only_selected_device_until_status_ready(self):
        self.provider.return_value = [port('other', serial_number=OTHER_SERIAL),
                                      port('target', serial_number=TARGET_SERIAL.lower())]
        self.status.side_effect = [OSError('synthetic transient'), TimeoutError(), FIRMWARE]
        self.assertEqual(flash.wait_for_application(TARGET_SERIAL, VERSION, timeout=1), 'target')
        self.assertEqual([call.args[0] for call in self.status.call_args_list], ['target'] * 3)

    def test_reconnect_timeout_and_duplicate_identity_fail(self):
        self.provider.return_value = [port('target')]
        self.status.side_effect = TimeoutError()
        with self.assertRaisesRegex(RuntimeError, 'same Tab5'):
            flash.wait_for_application(TARGET_SERIAL, VERSION, timeout=1)
        self.status.reset_mock()
        self.provider.return_value = [port('target'), port('duplicate')]
        with self.assertRaisesRegex(RuntimeError, 'Several application ports'):
            flash.wait_for_application(TARGET_SERIAL, VERSION, timeout=1)
        self.status.assert_not_called()

    def test_main_holds_existing_shared_lock_around_flash(self):
        root = Path(self.temporary)
        image = root / '.pio/build/tab5/firmware.factory.bin'
        image.parent.mkdir(parents=True)
        image.write_bytes(firmware_fixture())
        events = []
        lock = Mock()
        lock.__enter__ = Mock(side_effect=lambda: events.append('lock'))
        lock.__exit__ = Mock(side_effect=lambda *args: events.append('unlock'))
        with patch.object(flash, 'ROOT', root), patch.object(flash, 'serial_lock', return_value=lock) as shared:
            with patch.object(flash, 'flash', side_effect=lambda *args: events.append('flash')):
                flash.main(['--skip-build', '--port', 'selected-rom'])
        self.assertEqual(events, ['lock', 'flash', 'unlock'])
        shared.assert_called_once_with(10)

    def app_only(self):
        self.args.app_only = True
        application = self.image.with_name('firmware.bin')
        application.write_bytes(self.image.read_bytes()[0x10000:])
        return application

    def test_app_only_writes_exactly_the_application_and_verifies_reconnect(self):
        application = self.app_only()
        self.provider.side_effect = [[port('selected-rom', flash.ROM_PID)],
                                     [port('other', serial_number=OTHER_SERIAL), port('target-app')]]
        self.assertEqual(flash.flash(self.args, self.image), 'target-app')
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0][-1], 'read-mac')
        writes = [command for command in commands if 'write-flash' in command]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][writes[0].index('write-flash') + 1:], ['0x10000', application])
        self.assertNotIn(self.image, writes[0])
        self.assertIn('watchdog-reset', commands[-1])
        self.status.assert_called_once_with('target-app', 3)

    def test_app_only_rejects_invalid_application_before_any_usb_activity(self):
        application = self.app_only()
        correct = application.read_bytes()
        wrong_chip = bytearray(correct)
        struct.pack_into('<H', wrong_chip, 12, 9)
        changed_payload = bytearray(correct)
        changed_payload[-1] ^= 1
        for invalid in (b'', correct[:23], correct[:-1], correct + b'\xff',
                        bytes(changed_payload), bytes(wrong_chip),
                        correct + b'\xff' * (0x500001 - len(correct))):
            with self.subTest(size=len(invalid)):
                application.write_bytes(invalid)
                with self.assertRaises(RuntimeError):
                    flash.flash(self.args, self.image)
                self.provider.assert_not_called()
                self.runner.assert_not_called()
        application.unlink()
        with self.assertRaisesRegex(RuntimeError, 'Build firmware.bin'):
            flash.flash(self.args, self.image)
        self.provider.assert_not_called()
        self.runner.assert_not_called()

    def test_app_only_retains_complete_factory_validation(self):
        self.app_only()
        original = self.image.read_bytes()
        for offset in (0x2000, 0x8000, 0x10000):
            with self.subTest(offset=hex(offset)):
                corrupt = bytearray(original)
                corrupt[offset] = 0
                self.image.write_bytes(corrupt)
                with self.assertRaises(RuntimeError):
                    flash.flash(self.args, self.image)
                self.provider.assert_not_called()
                self.runner.assert_not_called()

    def test_app_only_rejects_changed_or_ambiguous_app_partitions(self):
        self.app_only()
        original = self.image.read_bytes()
        mutations = []
        for field, value in ((4, 0x20000), (8, 0x400000)):
            data = bytearray(original)
            struct.pack_into('<I', data, 0x8000 + field, value)
            mutations.append(data)
        for subtype, offset in ((0, 0x10000), (0x10, 0x520000)):
            data = bytearray(original)
            struct.pack_into('<HBBII16sI', data, 0x8040, 0x50AA, 0, subtype,
                             offset, 0x500000, b'extra-app', 0)
            mutations.append(data)
        for index, data in enumerate(mutations):
            with self.subTest(index=index):
                self.image.write_bytes(data)
                with self.assertRaises(RuntimeError):
                    flash.flash(self.args, self.image)
                self.provider.assert_not_called()
                self.runner.assert_not_called()

    def test_app_only_accepts_exact_capacity_but_not_one_byte_more(self):
        application = firmware_fixture()[0x10000:].ljust(0x500000, b'\xff')
        factory = firmware_fixture()[:0x10000] + application
        flash.inspect_firmware(factory, VERSION)
        self.assertEqual(flash.inspect_application(factory, application), 0x10000)
        with self.assertRaisesRegex(RuntimeError, 'exceeds its partition'):
            flash.inspect_application(factory, application + b'\xff')

    def test_app_only_still_rejects_wrong_rom_chip_identity(self):
        self.app_only()
        self.args.port = 'selected-app'
        self.provider.return_value = [port('selected-app')]
        self.runner.return_value = SimpleNamespace(stdout='MAC: aa:bb:cc:dd:ee:ff\n')
        with patch.object(flash, 'enter_rom', return_value='new-rom'):
            with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                flash.flash(self.args, self.image)
        self.assertEqual(self.runner.call_count, 1)
        self.status.assert_not_called()

    def test_app_only_still_requires_matching_version_after_restart(self):
        self.app_only()
        self.provider.side_effect = [[port('selected-rom', flash.ROM_PID)], [port('target-app')]]
        self.status.return_value = FIRMWARE + '-old'
        with self.assertRaisesRegex(RuntimeError, 'expected firmware version'):
            flash.flash(self.args, self.image)
        self.assertNotIn('verified', self.output.getvalue())

    def test_app_only_cannot_be_combined_with_font_image(self):
        self.app_only()
        self.args.font_image = Path('title-font.bin')
        with self.assertRaisesRegex(RuntimeError, 'cannot be combined'):
            flash.flash(self.args, self.image)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            flash.main(['--app-only', '--font-image', 'title-font.bin'])
        self.assertEqual(caught.exception.code, 2)
        self.provider.assert_not_called()
        self.runner.assert_not_called()


class StatusPrivacyTests(unittest.TestCase):
    def read(self, response, *, fail_write=False):
        clock = Clock()
        pending = deque(response)
        connection = Mock()

        def byte(_):
            if pending:
                return bytes([pending.popleft()])
            clock.sleep(0.1)
            return b''

        connection.read.side_effect = byte
        if fail_write:
            connection.write.side_effect = OSError('synthetic CDC failure')
        with patch.object(flash.serial, 'Serial', return_value=connection):
            with patch.object(flash.time, 'monotonic', clock.monotonic):
                try:
                    return flash.read_firmware_status('expected-port', 1)
                finally:
                    connection.close.assert_called_once()
                    connection.reset_input_buffer.assert_called_once()
                    connection.write.assert_called_once_with(b'STATUS\n')
                    self.assertTrue(connection.dtr)
                    self.assertFalse(connection.rts)
                    self.assertEqual(connection.port, 'expected-port')

    def test_fresh_status_returns_only_version_and_ignores_boot_noise(self):
        private = 'sensitive chat title'
        response = ('booting\n{bad json}\n' + '{"firmware":"' + FIRMWARE + '","title":"' + private + '"}\n').encode()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(self.read(response), FIRMWARE)
        self.assertNotIn(private, output.getvalue())

    def test_timeout_and_oversize_errors_never_echo_private_status(self):
        private = 'sensitive chat title'
        for response, expected in ((('{"title":"' + private + '"}\n').encode(), TimeoutError),
                                   ((private * 4000).encode(), RuntimeError)):
            with self.subTest(expected=expected), self.assertRaises(expected) as caught:
                self.read(response)
            self.assertNotIn(private, str(caught.exception))

    def test_connection_is_closed_on_write_failure(self):
        with self.assertRaises(OSError):
            self.read(b'', fail_write=True)


if __name__ == '__main__':
    unittest.main()
