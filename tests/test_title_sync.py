"""Synthetic title bridge checks; no connected hardware or user state is read."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import title_sync
import tab5_transport
import desktop_sync


class TitleSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tab5-title-test-")
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "state.json"
        self.db_path = self.root / "threads.sqlite"
        with sqlite3.connect(self.db_path) as database:
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, "
                "archived INTEGER NOT NULL DEFAULT 0, updated_at_ms INTEGER)"
            )
        self.assignments = {f"AG{slot:02d}": None for slot in range(6)}
        self.state = {
            "codex-micro-agent-source": "custom",
            "electron-persisted-atom-state": {
                "codex-micro-custom-agent-assignments": self.assignments
            },
        }

    def tearDown(self):
        self.temporary.cleanup()

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def insert_thread(self, thread_id, title, *, archived=0, updated_at=10):
        with sqlite3.connect(self.db_path) as database:
            database.execute(
                "INSERT INTO threads (id, name, archived, updated_at_ms) VALUES (?, ?, ?, ?)",
                (thread_id, title, archived, updated_at),
            )

    def assign(self, slot=0, thread_id="explicit-a", *, host_id="local", title="Old title"):
        self.assignments[f"AG{slot:02d}"] = {
            "hostId": host_id,
            "threadKey": "local:" + thread_id,
            "title": title,
        }

    def snapshot(self):
        self.save_state()
        return title_sync.build_snapshot(self.state_path, self.db_path)

    def assert_cleared(self, snapshot):
        self.assertEqual(snapshot.source, "unavailable")
        self.assertTrue(snapshot.reason)
        self.assertEqual([slot.id for slot in snapshot.slots], list(range(6)))
        self.assertTrue(all(slot.key == "" and slot.title == "" for slot in snapshot.slots))

    def test_explicit_ids_keep_slots_and_use_current_database_title(self):
        self.assign(4, "explicit-a", title="Stale persisted title")
        self.assign(1, "explicit-b")
        self.insert_thread("explicit-a", "Current 中文 title", updated_at=1)
        self.insert_thread("explicit-b", "Second current title", updated_at=2)
        self.insert_thread("newest-unassigned", "Must not occupy a slot", updated_at=999999)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.source, "custom")
        self.assertEqual(snapshot.slots[4].key, "local:explicit-a")
        self.assertEqual(snapshot.slots[4].title, "Current 中文 title")
        self.assertEqual(snapshot.slots[1].title, "Second current title")
        self.assertEqual(snapshot.slots[0].title, "")
        self.assertEqual(snapshot.configured, 2)
        self.assertEqual(snapshot.resolved, 2)
        self.assertNotIn("Must not occupy", json.dumps([slot.title for slot in snapshot.slots]))

    def test_empty_custom_slots_are_known_empty(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.source, "custom")
        self.assertEqual(len(snapshot.slots), 6)
        self.assertTrue(all(not slot.key and not slot.title for slot in snapshot.slots))

    def test_global_mode_has_priority_over_legacy_atom(self):
        self.state["electron-persisted-atom-state"]["codex-micro-agent-source"] = "recent"
        self.assertEqual(self.snapshot().source, "custom")
        self.state["codex-micro-agent-source"] = "recent"
        self.state["electron-persisted-atom-state"]["codex-micro-agent-source"] = "custom"
        self.assert_cleared(self.snapshot())

    def test_absent_global_mode_defaults_to_recent_and_clears(self):
        del self.state["codex-micro-agent-source"]
        self.state["electron-persisted-atom-state"]["codex-micro-agent-source"] = "custom"
        self.assert_cleared(self.snapshot())

    def test_desktop_custom_config_overrides_legacy_recent_mode(self):
        self.state["codex-micro-agent-source"] = "recent"
        self.assign()
        self.insert_thread("explicit-a", "Configured fixed chat")
        (self.root / "config.toml").write_text(
            '[desktop]\ncodex-micro-agent-source = "custom"\n', encoding="utf-8"
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot.source, "custom")
        self.assertEqual(snapshot.slots[0].key, "local:explicit-a")
        self.assertEqual(snapshot.slots[0].title, "Configured fixed chat")

    def test_desktop_recent_config_overrides_legacy_custom_mode(self):
        self.assign()
        self.insert_thread("explicit-a", "Must be cleared")
        self.save_state()
        config_path = self.root / "explicit-config.toml"
        config_path.write_text(
            '[desktop]\ncodex-micro-agent-source = "recent"\n', encoding="utf-8"
        )
        self.assert_cleared(
            title_sync.build_snapshot(self.state_path, self.db_path, config_path)
        )

    def test_malformed_desktop_config_clears_existing_assignment(self):
        self.assign()
        self.insert_thread("explicit-a", "Must not survive invalid configuration")
        (self.root / "config.toml").write_text(
            '[desktop\ncodex-micro-agent-source = "custom"\n', encoding="utf-8"
        )
        self.assert_cleared(self.snapshot())

    def test_dynamic_modes_never_guess_a_mapping(self):
        self.insert_thread("newest-unassigned", "Do not guess", updated_at=999999)
        for mode in ("recent", "pinned", "priority", "unknown"):
            with self.subTest(mode=mode):
                self.state["codex-micro-agent-source"] = mode
                self.assert_cleared(self.snapshot())

    def test_nonlocal_assignment_clears_the_batch(self):
        self.assign(host_id="remote-machine", title="Stale remote title")
        self.insert_thread("explicit-a", "Unrelated local same ID")
        self.assert_cleared(self.snapshot())

    def test_missing_or_archived_explicit_id_clears(self):
        self.assign()
        self.assert_cleared(self.snapshot())
        self.insert_thread("explicit-a", "Archived title", archived=1)
        self.assert_cleared(self.snapshot())

    def test_actions_and_malformed_maps_are_unavailable(self):
        for entry in (
            {"type": "command", "commandId": "recentThread1"},
            {"type": "skill", "skillName": "example"},
            {"hostId": "local", "threadKey": "remote:explicit-a", "title": "Wrong source"},
            {"hostId": "local", "threadKey": "local:"},
            "invalid assignment",
        ):
            with self.subTest(entry=entry):
                self.assignments["AG00"] = entry
                self.assert_cleared(self.snapshot())
        self.state["electron-persisted-atom-state"]["codex-micro-custom-agent-assignments"] = []
        self.assert_cleared(self.snapshot())

    def test_database_is_opened_read_only_and_unchanged(self):
        self.assign()
        self.insert_thread("explicit-a", "Read-only title")
        self.save_state()
        before = self.db_path.read_bytes()
        real_connect = sqlite3.connect
        calls = []
        denied_actions = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
        }

        def open_read_only(database, *args, **kwargs):
            calls.append((database, kwargs))
            self.assertIn("mode=ro", str(database))
            self.assertTrue(kwargs.get("uri"))
            connection = real_connect(database, *args, **kwargs)
            connection.set_authorizer(
                lambda action, *_: sqlite3.SQLITE_DENY
                if action in denied_actions else sqlite3.SQLITE_OK
            )
            return connection

        with mock.patch.object(title_sync.sqlite3, "connect", side_effect=open_read_only):
            snapshot = title_sync.build_snapshot(self.state_path, self.db_path)
        self.assertEqual(snapshot.source, "custom")
        self.assertTrue(calls)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_unicode_titles_are_normalized_and_fit_command_limit(self):
        title = title_sync.normalize_title("  中文\n\t🚀\x00 title  ")
        self.assertIn("中文", title)
        self.assertIn("🚀", title)
        self.assertEqual(title.strip(), title)
        self.assertNotIn("  ", title)
        self.assertFalse(any(ord(character) < 32 for character in title))
        long_title = title_sync.normalize_title("🧪中文" * 1000)
        self.assertLessEqual(len(long_title.encode("utf-8")), 191)
        self.assertEqual(long_title.encode("utf-8").decode("utf-8"), long_title)
        slots = tuple(
            title_sync.Slot(slot, "local:" + ("a" * 88) + str(slot), long_title)
            for slot in range(6)
        )
        snapshot = title_sync.Snapshot("custom", slots, configured=6, resolved=6)
        command = title_sync.encode_command(snapshot, 42)
        self.assertIsInstance(command, bytes)
        self.assertLessEqual(len(command), 6144)
        self.assertTrue(command.startswith(b"TITLES "))
        self.assertTrue(command.endswith(b"\n"))
        payload = json.loads(command[len(b"TITLES "):-1])
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["seq"], 42)
        self.assertEqual(payload["source"], "custom")
        self.assertEqual([slot["id"] for slot in payload["slots"]], list(range(6)))
        self.assertEqual(payload["slots"][0]["title"], long_title)

    def test_dry_run_hides_titles_unless_explicitly_requested(self):
        self.assign()
        self.insert_thread("explicit-a", "Private synthetic title")
        self.save_state()
        command = [
            sys.executable, str(TOOLS / "title_sync.py"), "--dry-run",
            "--state", str(self.state_path), "--db", str(self.db_path),
        ]
        hidden = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(hidden.returncode, 0, hidden.stderr)
        self.assertNotIn("Private synthetic title", hidden.stdout)
        self.assertEqual(json.loads(hidden.stdout)["source"], "custom")
        shown = subprocess.run(command + ["--show-titles"], capture_output=True, text=True, check=False)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("Private synthetic title", shown.stdout)

    def test_escaped_titles_and_long_keys_stay_inside_wire_budget(self):
        original_title = ('"\\' * 95) + '"'
        slots = tuple(
            title_sync.Slot(slot, "local:" + ("a" * 88) + str(slot), original_title)
            for slot in range(6)
        )
        snapshot = title_sync.Snapshot("custom", slots, configured=6, resolved=6)
        command = title_sync.encode_command(snapshot, 0xFFFFFFFF)
        self.assertLessEqual(len(command), 6144)
        payload = json.loads(command[len(b"TITLES "):-1])
        self.assertEqual(payload["seq"], 0xFFFFFFFF)
        for expected, actual in zip(slots, payload["slots"]):
            self.assertEqual(actual["key"], expected.key)
            self.assertEqual(len(actual["key"].encode("utf-8")), 95)
            self.assertTrue(actual["title"])
            self.assertLessEqual(len(actual["title"].encode("utf-8")), 191)
            self.assertTrue(original_title.startswith(actual["title"]))

    def test_configured_identity_beyond_firmware_limit_clears(self):
        identifier = "a" * 90
        self.assign(thread_id=identifier)
        self.insert_thread(identifier, "Correct row with oversized explicit key")
        self.assertEqual(len(("local:" + identifier).encode("utf-8")), 96)
        self.assert_cleared(self.snapshot())

    def test_encoder_rejects_identity_beyond_firmware_limit(self):
        slots = (title_sync.Slot(0, "local:" + ("a" * 90), "Oversized key"),) + tuple(
            title_sync.Slot(slot) for slot in range(1, 6)
        )
        snapshot = title_sync.Snapshot("custom", slots, configured=1, resolved=1)
        with self.assertRaises(ValueError):
            title_sync.encode_command(snapshot, 1)


class AckReliabilityTests(unittest.TestCase):
    class Serial:
        def __init__(self, clock, chunks):
            self.clock = clock
            self.chunks = list(chunks)
            self.writes = []
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def write(self, data):
            self.writes.append(data)

        def flush(self):
            pass

        def read_until(self, *args):
            self.clock[0] += 0.2
            return self.chunks.pop(0) if self.chunks else b""

    def send(self, chunks, *, ack_timeout=1.5):
        clock = [0.0]
        connection = self.Serial(clock, chunks)
        with mock.patch.object(title_sync, "open_serial", return_value=connection) as opened, \
                mock.patch.object(title_sync.time, "monotonic", side_effect=lambda: clock[0]):
            title_sync.send_snapshots(title_sync.unavailable("Synthetic fixture"),
                                      desktop_sync.unavailable(), 37,
                                      timeout=0.5, ack_timeout=ack_timeout)
        return connection, opened, clock[0]

    def test_ack_fragments_are_retained_until_newline(self):
        connection, opened, _ = self.send([
            b"TITLES_", b"OK ", b"37\r", b"\n",
            b"HOST_CONFIG_OK ", b"37\n",
        ])
        self.assertEqual(len(connection.writes), 2)
        self.assertTrue(connection.closed)
        opened.assert_called_once_with(None, 0.5)

    def test_normal_render_delay_can_exceed_lock_wait_without_losing_ack(self):
        connection, opened, elapsed = self.send([
            b"", b"", b"", b"TITLES_OK 37\n", b"HOST_CONFIG_OK 37\n",
        ])
        self.assertGreater(elapsed, 0.5)
        self.assertLess(elapsed, title_sync.ACK_TIMEOUT_SECONDS * 2)
        opened.assert_called_once_with(None, 0.5)
        self.assertEqual(len(connection.writes), 2)

    def test_late_fragmented_title_ack_cannot_acknowledge_the_config_packet(self):
        clock = [0.0]
        connection = self.Serial(clock, [b"TITLES_OK ", b"", b"", b"37\n",
                                         b"HOST_CONFIG_OK ", b"37\n"])
        with mock.patch.object(title_sync, "open_serial", return_value=connection), \
                mock.patch.object(title_sync.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError) as caught:
                title_sync.send_snapshots(title_sync.unavailable("Synthetic fixture"),
                                          desktop_sync.unavailable(), 37,
                                          ack_timeout=0.5)
        self.assertEqual(caught.exception.packet, "TITLES")
        self.assertEqual(caught.exception.failed_packets, (("TITLES", "ack-timeout"),))
        self.assertEqual(len(connection.writes), 2)
        self.assertTrue(connection.writes[1].startswith(b"HOST_CONFIG "))
        self.assertTrue(connection.closed)

    def test_oversized_unterminated_input_is_discarded_before_resynchronizing(self):
        reader = title_sync._AckLines()
        self.assertEqual(reader.feed(b"x" * 8193), [])
        self.assertEqual(reader.feed(b"TITLES_OK 37\nHOST_CONFIG_OK 37\n"),
                         [b"HOST_CONFIG_OK 37"])
        self.assertFalse(reader.pending)

    def test_invalid_ack_budgets_fail_before_any_serial_open(self):
        for value in (0, -1, float("inf"), float("nan"), True, "1.5"):
            with self.subTest(value=value), mock.patch.object(title_sync, "open_serial") as opened:
                with self.assertRaises(ValueError):
                    title_sync.send_snapshots(title_sync.unavailable("Synthetic fixture"),
                                              desktop_sync.unavailable(), 37,
                                              ack_timeout=value)
                opened.assert_not_called()

    def test_failure_metadata_cannot_log_raw_serial_or_private_content(self):
        secret = "synthetic-private-title-or-path"
        self.assertEqual(title_sync._failure_details(TimeoutError(secret)),
                         {"packet": "connection", "failure": "unavailable"})
        error = RuntimeError(secret)
        error.packet, error.failure = secret, secret
        error.failed_packets = ((secret, secret), ("TITLES", "ack-timeout"))
        details = title_sync._failure_details(error)
        self.assertNotIn(secret, json.dumps(details))
        self.assertEqual(details["failed_packets"], [{"packet": "TITLES", "failure": "ack-timeout"}])


class Tab5TransportSelectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(tab5_transport.os.environ, {"TAB5_SERIAL_NUMBER": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def port(device, *, vid=0x303A, pid=0x8360, serial_number="TAB5-0123456789AB"):
        return SimpleNamespace(
            device=device, vid=vid, pid=pid, serial_number=serial_number
        )

    def test_selects_only_exact_tab5_among_s3_rom_and_other_devices(self):
        devices = [
            self.port("/dev/cu.s3", pid=0x1001, serial_number="S3-OTHER"),
            self.port("/dev/cu.p4-rom", pid=0x1001),
            self.port("/dev/cu.other-micro", serial_number="TAB5-OTHER"),
            self.port("/dev/cu.other-vendor", vid=0x1234),
            self.port("/dev/cu.tab5"),
        ]
        with mock.patch.object(tab5_transport.list_ports, "comports", return_value=devices):
            self.assertEqual(tab5_transport.serial_port(), "/dev/cu.tab5")

    def test_s3_rom_and_wrong_serial_are_never_fallback_targets(self):
        invalid_devices = (
            self.port("/dev/cu.s3", pid=0x1001, serial_number="S3-OTHER"),
            self.port("/dev/cu.p4-rom", pid=0x1001),
            self.port("/dev/cu.other-micro", serial_number="TAB5-OTHER"),
        )
        for invalid in invalid_devices:
            with self.subTest(device=invalid.device):
                with mock.patch.object(tab5_transport.list_ports, "comports", return_value=[invalid]):
                    with self.assertRaises(RuntimeError):
                        tab5_transport.serial_port()
                    with self.assertRaises(RuntimeError):
                        tab5_transport.serial_port(invalid.device)

    def test_requested_wrong_port_is_rejected_even_when_tab5_exists(self):
        devices = [
            self.port("/dev/cu.tab5"),
            self.port("/dev/cu.s3", pid=0x1001, serial_number="S3-OTHER"),
        ]
        with mock.patch.object(tab5_transport.list_ports, "comports", return_value=devices):
            with self.assertRaises(RuntimeError):
                tab5_transport.serial_port("/dev/cu.s3")
            with self.assertRaises(RuntimeError):
                tab5_transport.serial_port("/dev/cu.not-present")

    def test_explicit_matching_tab5_port_is_accepted(self):
        devices = [self.port("/dev/cu.tab5")]
        with mock.patch.object(tab5_transport.list_ports, "comports", return_value=devices):
            self.assertEqual(tab5_transport.serial_port("/dev/cu.tab5"), "/dev/cu.tab5")

    def test_ambiguous_duplicate_identity_is_rejected(self):
        devices = [self.port("/dev/cu.tab5-a"), self.port("/dev/cu.tab5-b")]
        with mock.patch.object(tab5_transport.list_ports, "comports", return_value=devices):
            with self.assertRaises(RuntimeError):
                tab5_transport.serial_port()

    def test_distinct_tab5_devices_require_selection(self):
        devices = [self.port("/dev/cu.tab5-a"),
                   self.port("/dev/cu.tab5-b", serial_number="TAB5-ABCDEF012345")]
        with mock.patch.object(tab5_transport.list_ports, "comports", return_value=devices):
            with self.assertRaises(RuntimeError):
                tab5_transport.serial_port()
            self.assertEqual(tab5_transport.serial_port("/dev/cu.tab5-b"), "/dev/cu.tab5-b")
            with mock.patch.dict(tab5_transport.os.environ, {"TAB5_SERIAL_NUMBER": "tab5-abcdef012345"}):
                self.assertEqual(tab5_transport.serial_port(), "/dev/cu.tab5-b")
                with self.assertRaises(RuntimeError):
                    tab5_transport.serial_port("/dev/cu.tab5-a")

    def test_invalid_serial_filter_cannot_open_any_device(self):
        with mock.patch.dict(tab5_transport.os.environ, {"TAB5_SERIAL_NUMBER": "S3-OTHER"}):
            with self.assertRaises(ValueError):
                tab5_transport.serial_port()

    def test_missing_serial_is_rejected(self):
        with mock.patch.object(tab5_transport.list_ports, "comports", return_value=[
            self.port("/dev/cu.unidentified", serial_number=None)
        ]):
            with self.assertRaises(RuntimeError):
                tab5_transport.serial_port()


if __name__ == "__main__":
    unittest.main()
