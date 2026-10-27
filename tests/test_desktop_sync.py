"""Desktop-default contracts using synthetic files; no user state or USB reads."""

from __future__ import annotations

from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import desktop_sync
import title_sync


LAYOUT_KEY = "codex-micro-layout"
DEFAULT_ACTIONS = ("fast", "approve", "reject", "fork", "voice", "none", "send")
UNKNOWN_ACTIONS = ("unknown",) * 7
REQUIRED_SLOTS = ("ACT06", "ACT07", "ACT08", "ACT09", "ACT10_ACT11", "ACT12")
PRIVATE = "SYNTHETIC_PRIVATE_DESKTOP_DATA_NEVER_EMIT"


def minimal_layout() -> dict:
    # ACT10/ACT11 are deliberately absent: the desktop gives them defaults.
    return {
        "version": 1,
        "slots": {
            "ACT06": {"keycapId": "FAST"},
            "ACT07": {"keycapId": "APPR"},
            "ACT08": {"keycapId": "REJ"},
            "ACT09": {"keycapId": "SPLIT"},
            "ACT10_ACT11": {"keycapId": "MIC"},
            "ACT12": {"keycapId": "CODEX"},
        },
    }


def toml_value(value) -> str:
    """Serialize just the synthetic TOML fixture types, independently of the reader."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            json.dumps(key) + " = " + toml_value(item) for key, item in value.items()
        ) + "}"
    raise TypeError("Unsupported synthetic TOML fixture value")


class DesktopSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tab5-desktop-test-")
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "state.json"
        self.config_path = self.root / "config.toml"
        self.state = {}
        self.save_state()

    def tearDown(self):
        self.temporary.cleanup()

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def write_config(self, values: dict, path: Path | None = None):
        path = path or self.config_path
        lines = []
        for key, value in values.items():
            if key != "desktop":
                lines.append(json.dumps(key) + " = " + toml_value(value))
        if "desktop" in values:
            desktop = values["desktop"]
            if isinstance(desktop, dict):
                lines.append("[desktop]")
                lines.extend(json.dumps(key) + " = " + toml_value(value)
                             for key, value in desktop.items())
            else:
                lines.append("desktop = " + toml_value(desktop))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def snapshot(self, config_path: Path | None = None):
        self.save_state()
        return desktop_sync.build_snapshot(self.state_path, config_path)

    def assert_unknown_layout(self, snapshot):
        self.assertEqual(snapshot.encoder, "unknown")
        self.assertEqual(snapshot.voice, "unknown")
        self.assertEqual(tuple(snapshot.actions), UNKNOWN_ACTIONS)

    def assert_unavailable(self, snapshot):
        self.assertEqual(snapshot.source, "unavailable")
        self.assertEqual(snapshot.tier, "unknown")
        self.assert_unknown_layout(snapshot)

    def test_valid_empty_state_uses_desktop_layout_defaults_not_a_tier_guess(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.source, "local-config")
        self.assertEqual(snapshot.tier, "unknown")
        self.assertEqual(snapshot.encoder, "composer-navigation")
        self.assertEqual(snapshot.voice, "push-to-talk")
        self.assertEqual(tuple(snapshot.actions), DEFAULT_ACTIONS)

    def test_root_tiers_are_saved_defaults_and_missing_default_or_flex_stay_unknown(self):
        for raw, expected in (
            ("priority", "fast"), ("fast", "fast"), ("ultrafast", "ultrafast"),
            ("standard", "unknown"), ("default", "unknown"),
            ("flex", "unknown"), ("unrecognized", "unknown"), ("FAST", "unknown"),
        ):
            with self.subTest(raw=raw):
                self.write_config({"service_tier": raw})
                self.assertEqual(self.snapshot().tier, expected)
        for raw in (True, 1, [], {}):
            with self.subTest(invalid_type=raw):
                self.write_config({"service_tier": raw})
                self.assertEqual(self.snapshot().tier, "unknown")

    def test_no_active_chat_or_model_default_is_inferred_from_unrelated_metadata(self):
        self.state.update({"service_tier": "fast", "activeThreadId": PRIVATE,
                           "electron-persisted-atom-state": {"service_tier": "ultrafast"}})
        self.write_config({"model": "synthetic-model-with-unspecified-default",
                           "profiles": {"unselected": {"service_tier": "fast"}}})
        snapshot = self.snapshot()
        self.assertEqual(snapshot.tier, "unknown")
        self.assertEqual(desktop_sync.summary(snapshot)["scope"], "configuration-defaults")

    def test_only_explicit_profile_overrides_root_and_absent_profile_tier_inherits(self):
        self.write_config({"service_tier": "standard", "profile": "selected",
                           "profiles": {"selected": {"service_tier": "ultrafast"},
                                        "other": {"service_tier": "fast"}}})
        self.assertEqual(self.snapshot().tier, "ultrafast")
        self.write_config({"service_tier": "fast", "profile": "selected",
                           "profiles": {"selected": {"model": "synthetic-model"}}})
        self.assertEqual(self.snapshot().tier, "fast")

    def test_missing_or_malformed_explicit_profile_never_uses_root_as_a_guess(self):
        for selected, profiles in (
            ("absent", {}), ("", {}), (True, {}), ([], {}), ({}, {}),
            ("selected", []), ("selected", {"selected": "invalid"}),
            ("selected", {"selected": {"service_tier": False}}),
        ):
            with self.subTest(selected=selected, profiles=profiles):
                self.write_config({"service_tier": "fast", "profile": selected,
                                   "profiles": profiles})
                self.assertEqual(self.snapshot().tier, "unknown")

    def test_toml_layout_overrides_global_layout_without_consulting_legacy_atom(self):
        global_layout = minimal_layout()
        global_layout["slots"]["ACT06"]["commandId"] = "navigateBack"
        self.state[LAYOUT_KEY] = global_layout
        self.state["electron-persisted-atom-state"] = {
            LAYOUT_KEY: {"version": 999}, "private-fixture": PRIVATE,
        }
        self.assertEqual(self.snapshot().actions[0], "back")
        preferred = minimal_layout()
        preferred["slots"]["ACT06"]["commandId"] = "toggleSidebar"
        self.write_config({"desktop": {LAYOUT_KEY: preferred}})
        self.assertEqual(self.snapshot().actions[0], "sidebar")
        self.state[LAYOUT_KEY] = []
        self.assertEqual(self.snapshot().actions[0], "sidebar")

    def test_explicit_config_path_has_priority_over_config_beside_state(self):
        adjacent = minimal_layout()
        adjacent["slots"]["ACT06"]["commandId"] = "navigateBack"
        self.write_config({"desktop": {LAYOUT_KEY: adjacent}})
        preferred = minimal_layout()
        preferred["slots"]["ACT06"]["commandId"] = "navigateForward"
        explicit = self.write_config({"service_tier": "ultrafast",
                                      "desktop": {LAYOUT_KEY: preferred}},
                                     self.root / "explicit.toml")
        snapshot = self.snapshot(explicit)
        self.assertEqual(snapshot.actions[0], "forward")
        self.assertEqual(snapshot.tier, "ultrafast")

    def test_invalid_toml_layout_does_not_fall_back_but_preserves_independent_tier(self):
        self.state[LAYOUT_KEY] = minimal_layout()
        for invalid in (False, "invalid", [], {}, {"version": 2}):
            with self.subTest(invalid=invalid):
                self.write_config({"service_tier": "priority",
                                   "desktop": {LAYOUT_KEY: invalid}})
                snapshot = self.snapshot()
                self.assertEqual(snapshot.source, "local-config")
                self.assertEqual(snapshot.tier, "fast")
                self.assert_unknown_layout(snapshot)

    def test_malformed_toml_clears_all_fields_even_when_global_layout_is_valid(self):
        self.state[LAYOUT_KEY] = minimal_layout()
        self.save_state()
        for content in (b'[desktop\n', b'service_tier = "fast"\n[desktop\n', b'\xff\xfe'):
            with self.subTest(content=content):
                self.config_path.write_bytes(content)
                self.assert_unavailable(desktop_sync.build_snapshot(self.state_path))

    def test_missing_both_sources_is_unavailable(self):
        self.state_path.unlink()
        self.assert_unavailable(desktop_sync.build_snapshot(self.state_path))

    def test_bad_state_with_readable_toml_preserves_tier_but_not_layout_guess(self):
        self.write_config({"service_tier": "fast"})
        for content in (b'{invalid', b'[]', b'null', b'\xff'):
            with self.subTest(content=content):
                self.state_path.write_bytes(content)
                snapshot = desktop_sync.build_snapshot(self.state_path)
                self.assertEqual(snapshot.source, "local-config")
                self.assertEqual(snapshot.tier, "fast")
                self.assert_unknown_layout(snapshot)
        self.state_path.unlink()
        self.assert_unknown_layout(desktop_sync.build_snapshot(self.state_path))

    def test_authoritative_toml_layout_does_not_require_a_readable_global_state(self):
        self.write_config({"service_tier": "fast",
                           "desktop": {LAYOUT_KEY: minimal_layout()}})
        self.state_path.write_text("invalid-json", encoding="utf-8")
        snapshot = desktop_sync.build_snapshot(self.state_path)
        self.assertEqual(snapshot.tier, "fast")
        self.assertEqual(tuple(snapshot.actions), DEFAULT_ACTIONS)

    def test_bad_state_without_readable_config_is_unavailable(self):
        for content in (b'{invalid', b'[]', b'null', b'\xff'):
            with self.subTest(content=content):
                self.state_path.write_bytes(content)
                self.assert_unavailable(desktop_sync.build_snapshot(self.state_path))

    def test_required_slots_missing_never_recover_plausible_default_actions(self):
        for name in REQUIRED_SLOTS:
            with self.subTest(name=name):
                layout = minimal_layout()
                del layout["slots"][name]
                self.state[LAYOUT_KEY] = layout
                self.assert_unknown_layout(self.snapshot())

    def test_layout_version_types_and_field_types_fail_closed(self):
        cases = [("version", value) for value in (True, "1", 0, 2, None)]
        cases += [("slots", value) for value in (None, [], "slots")]
        cases += [("encoderMode", value) for value in (True, [], {}, "unknown", "bad")]
        cases += [("voiceButtonMode", value) for value in (False, [], {}, "unknown", "bad")]
        cases += [("separateMicrophoneKeys", value) for value in (0, 1, "false", [], {})]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                layout = minimal_layout()
                layout[field] = value
                self.state[LAYOUT_KEY] = layout
                self.assert_unknown_layout(self.snapshot())

    def test_unhashable_or_invalid_slot_keycap_is_unavailable_not_an_exception(self):
        for value in ([], {}, ["FAST"], {"FAST": True}, None, True, 7, "NEW_UNKNOWN_KEYCAP"):
            with self.subTest(value=value):
                layout = minimal_layout()
                layout["slots"]["ACT06"]["keycapId"] = value
                self.state[LAYOUT_KEY] = layout
                self.assert_unknown_layout(self.snapshot())

    def test_invalid_slot_shapes_command_types_and_actions_fail_closed(self):
        replacements = [None, [], "slot", {}, {"keycapId": "FAST", "commandId": []},
                        {"keycapId": "FAST", "commandId": {}},
                        {"keycapId": "FAST", "action": None},
                        {"keycapId": "FAST", "action": {"type": [], "commandId": "navigateBack"}},
                        {"keycapId": "FAST", "action": {"type": "command", "commandId": []}},
                        {"keycapId": "FAST", "action": {"type": "command", "commandId": {}}},
                        {"keycapId": "FAST", "action": {"type": "command", "commandId": ""}}]
        for slot in replacements:
            with self.subTest(slot=slot):
                layout = minimal_layout()
                layout["slots"]["ACT06"] = slot
                self.state[LAYOUT_KEY] = layout
                self.assert_unknown_layout(self.snapshot())

    def test_command_action_precedence_is_explicit_and_unknown_command_does_not_use_keycap(self):
        layout = minimal_layout()
        layout["slots"]["ACT06"].update({"commandId": "approval.approve",
            "action": {"type": "command", "commandId": "navigateBack"}})
        layout["slots"]["ACT07"]["commandId"] = "composer.submit"
        layout["slots"]["ACT08"]["commandId"] = "unrecognized.synthetic.command"
        self.state[LAYOUT_KEY] = layout
        self.assertEqual(tuple(self.snapshot().actions[:3]), ("back", "send", "unknown"))

    def test_native_command_mappings_are_bounded_known_semantics(self):
        for command, expected in (
            ("composer.toggleFastMode", "fast"), ("approval.approve", "approve"),
            ("approval.decline", "reject"), ("forkThread", "fork"),
            ("composer.submit", "send"), ("composer.togglePlanMode", "plan"),
            ("toggleSidebar", "sidebar"), ("navigateBack", "back"),
            ("navigateForward", "forward"),
        ):
            with self.subTest(command=command):
                layout = minimal_layout()
                layout["slots"]["ACT06"]["action"] = {"type": "command", "commandId": command}
                self.state[LAYOUT_KEY] = layout
                self.assertEqual(self.snapshot().actions[0], expected)

    def test_shared_voice_routes_only_shared_slot_and_suppresses_aux_key(self):
        layout = minimal_layout()
        layout["slots"]["ACT10"] = {"keycapId": "FAST", "commandId": "navigateBack"}
        layout["slots"]["ACT11"] = {"keycapId": "TERM", "commandId": "navigateForward"}
        layout["slots"]["ACT10_ACT11"]["commandId"] = "toggleSidebar"
        self.state[LAYOUT_KEY] = layout
        self.assertEqual(tuple(self.snapshot().actions[4:6]), ("sidebar", "none"))

    def test_separate_voice_uses_individual_slots_and_keeps_optional_defaults(self):
        layout = minimal_layout()
        layout["separateMicrophoneKeys"] = True
        self.state[LAYOUT_KEY] = layout
        self.assertEqual(tuple(self.snapshot().actions[4:6]), ("voice", "none"))
        layout["slots"]["ACT10"] = {"keycapId": "CODEX"}
        layout["slots"]["ACT11"] = {"keycapId": "TERM"}
        self.assertEqual(tuple(self.snapshot().actions[4:6]), ("send", "aux"))

    def test_supported_encoder_and_voice_modes_do_not_change_action_identity(self):
        for encoder in ("composer-navigation", "reasoning", "conversation-scroll", "custom"):
            for voice in ("push-to-talk", "realtime"):
                with self.subTest(encoder=encoder, voice=voice):
                    layout = minimal_layout()
                    layout.update(encoderMode=encoder, voiceButtonMode=voice)
                    self.state[LAYOUT_KEY] = layout
                    snapshot = self.snapshot()
                    self.assertEqual((snapshot.encoder, snapshot.voice), (encoder, voice))
                    self.assertEqual(tuple(snapshot.actions), DEFAULT_ACTIONS)

    def test_empty_aux_and_custom_keycaps_preserve_their_distinct_meanings(self):
        for keycap, expected in (
            ("EMPT1", "none"), ("EMPT2", "none"), ("EMPT3", "none"),
            ("EMPT4", "none"), ("EMPT5", "none"),
            ("BUG", "aux"), ("TERM", "aux"), ("OAI", "aux"),
            ("YOLO", "custom"), ("YEET", "custom"),
        ):
            with self.subTest(keycap=keycap):
                layout = minimal_layout()
                layout["slots"]["ACT06"]["keycapId"] = keycap
                self.state[LAYOUT_KEY] = layout
                self.assertEqual(self.snapshot().actions[0], expected)

    def test_unused_native_mapping_fields_still_require_valid_schema(self):
        for field, mappings in (
            ("analogStick", {}), ("encoder", []),
            ("analogStick", {direction: {"type": "command", "commandId": []}
                             for direction in ("up", "right", "down", "left")}),
        ):
            with self.subTest(field=field):
                layout = minimal_layout()
                layout[field] = mappings
                self.state[LAYOUT_KEY] = layout
                self.assert_unknown_layout(self.snapshot())

    def test_private_custom_actions_profiles_urls_and_ids_never_leave_as_text(self):
        layout = minimal_layout()
        layout["slots"]["ACT06"]["action"] = {
            "type": "skill", "skillName": PRIVATE, "skillPath": "/synthetic/" + PRIVATE,
        }
        layout["slots"]["ACT07"]["action"] = {
            "type": "composer-text", "text": PRIVATE + " https://synthetic.invalid/private",
        }
        layout["slots"]["ACT08"]["commandId"] = PRIVATE
        self.state.update({LAYOUT_KEY: layout, "activeThreadId": PRIVATE})
        self.write_config({"profile": PRIVATE, "profiles": {PRIVATE: {"service_tier": "priority"}}})
        snapshot = self.snapshot()
        self.assertEqual(tuple(snapshot.actions[:3]), ("custom", "custom", "unknown"))
        encoded = desktop_sync.encode_command(snapshot, 1)
        shown = json.dumps(desktop_sync.summary(snapshot))
        for private in (PRIVATE, "https://synthetic.invalid/private", "/synthetic/"):
            self.assertNotIn(private.encode(), encoded)
            self.assertNotIn(private, shown)
        self.assertEqual(json.loads(encoded[len(b"HOST_CONFIG "):])["tier"], "fast")

    def test_invalid_private_configuration_only_emits_generic_reason(self):
        layout = minimal_layout()
        layout["slots"]["ACT06"]["keycapId"] = PRIVATE
        self.state[LAYOUT_KEY] = layout
        snapshot = self.snapshot()
        self.assert_unknown_layout(snapshot)
        self.assertNotIn(PRIVATE, json.dumps(desktop_sync.summary(snapshot)))
        self.assertNotIn(PRIVATE.encode(), desktop_sync.encode_command(snapshot, 1))


class DesktopWireTests(unittest.TestCase):
    @staticmethod
    def snapshot(**changes):
        values = {"source": "local-config", "tier": "fast", "encoder": "reasoning",
                  "voice": "realtime", "actions": DEFAULT_ACTIONS}
        values.update(changes)
        return desktop_sync.DesktopSnapshot(**values)

    def test_unavailable_is_an_explicit_all_unknown_clear_packet(self):
        snapshot = desktop_sync.unavailable()
        self.assertEqual(snapshot.source, "unavailable")
        self.assertEqual((snapshot.tier, snapshot.encoder, snapshot.voice), ("unknown",) * 3)
        self.assertEqual(tuple(snapshot.actions), UNKNOWN_ACTIONS)
        payload = json.loads(desktop_sync.encode_command(snapshot, 0)[len(b"HOST_CONFIG "):])
        self.assertEqual(payload["actions"], ["unknown"] * 7)

    def test_wire_is_versioned_bounded_ascii_and_omits_reason(self):
        snapshot = self.snapshot(reason=PRIVATE)
        for sequence in (0, 1, 0xFFFFFFFF):
            command = desktop_sync.encode_command(snapshot, sequence)
            self.assertIsInstance(command, bytes)
            self.assertTrue(command.startswith(b"HOST_CONFIG "))
            self.assertTrue(command.endswith(b"\n"))
            self.assertEqual(command.count(b"\n"), 1)
            self.assertLessEqual(len(command), 6144)
            command.decode("ascii")
            payload = json.loads(command[len(b"HOST_CONFIG "):])
            self.assertEqual(set(payload), {"v", "seq", "source", "tier", "encoder", "voice", "actions"})
            self.assertEqual((payload["v"], payload["seq"]), (1, sequence))
            self.assertEqual(payload["actions"], list(DEFAULT_ACTIONS))
            self.assertNotIn(PRIVATE.encode(), command)

    def test_sequence_requires_exact_uint32_not_bool_or_coercion(self):
        for value in (-1, 0x100000000, True, False, 1.0, "1", None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                desktop_sync.encode_command(self.snapshot(), value)

    def test_all_documented_enum_values_are_encodable_without_extra_payload(self):
        for field, values in (
            ("tier", ("unknown", "standard", "fast", "ultrafast")),
            ("encoder", ("unknown", "composer-navigation", "reasoning", "conversation-scroll", "custom")),
            ("voice", ("unknown", "push-to-talk", "realtime")),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    payload = json.loads(desktop_sync.encode_command(self.snapshot(**{field: value}), 9)[len(b"HOST_CONFIG "):])
                    self.assertEqual(payload[field], value)
        for action in ("unknown", "none", "fast", "approve", "reject", "fork", "voice", "aux",
                       "send", "plan", "sidebar", "back", "forward", "custom"):
            with self.subTest(action=action):
                payload = json.loads(desktop_sync.encode_command(self.snapshot(actions=(action,) * 7), 10)[len(b"HOST_CONFIG "):])
                self.assertEqual(payload["actions"], [action] * 7)

    def test_wire_rejects_unknown_enum_types_and_raw_text_injection(self):
        for field in ("source", "tier", "encoder", "voice"):
            for value in (PRIVATE, "fast\nHOST_CONFIG {}", None, True, 1, [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    desktop_sync.encode_command(self.snapshot(**{field: value}), 1)
        for actions in ((), DEFAULT_ACTIONS[:-1], DEFAULT_ACTIONS + ("none",),
                        "unknown", None, 7, ("unknown",) * 6 + (PRIVATE,),
                        ("unknown",) * 6 + ([],), ("unknown",) * 6 + ({},)):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                desktop_sync.encode_command(self.snapshot(actions=actions), 1)

    def test_unavailable_cannot_retain_a_previous_known_field(self):
        base = {"source": "unavailable", "tier": "unknown", "encoder": "unknown",
                "voice": "unknown", "actions": UNKNOWN_ACTIONS}
        for field, value in (("tier", "fast"), ("encoder", "reasoning"),
                             ("voice", "realtime"), ("actions", DEFAULT_ACTIONS)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                desktop_sync.encode_command(desktop_sync.DesktopSnapshot(**{**base, field: value}), 1)


def synthetic_titles(title="Synthetic fixed chat"):
    return title_sync.Snapshot(
        "custom",
        (title_sync.Slot(0, "local:synthetic-fixed", title),)
        + tuple(title_sync.Slot(index) for index in range(1, 6)),
        configured=1,
        resolved=1,
    )


class FakeSerial:
    """Scripted context and byte stream; never delegates to a serial device."""

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.trace = []
        self.writes = []
        self.entered = 0
        self.closed = False
        self.exit_error = None

    def __enter__(self):
        self.entered += 1
        self.trace.append(("open", None))
        return self

    def __exit__(self, error_type, error, traceback):
        self.closed = True
        self.exit_error = error_type
        self.trace.append(("close", None))
        return False

    def write(self, command):
        if self.entered != 1 or self.closed:
            raise AssertionError("Write outside the one live serial context")
        self.writes.append(command)
        self.trace.append(("write", command))
        return len(command)

    def flush(self):
        self.trace.append(("flush", None))

    def read_until(self, delimiter, limit):
        if delimiter != b"\n" or limit != 8192:
            raise AssertionError("Unexpected bounded line read")
        line = self.lines.pop(0) if self.lines else b""
        self.trace.append(("read", line))
        return line


class DesktopTransportTests(unittest.TestCase):
    def setUp(self):
        self.titles = synthetic_titles()
        self.desktop = DesktopWireTests.snapshot()

    def send(self, connection, *, seq=37, timeout=0.5, desktop=None, title_only=False):
        # Advance only the fake deadline; timeout checks cannot block this suite.
        ticks = iter(index / 100 for index in range(10000))
        with mock.patch.object(title_sync, "open_serial", return_value=connection) as opened, \
                mock.patch.object(title_sync.time, "monotonic", side_effect=lambda: next(ticks)):
            if title_only:
                title_sync.send_snapshot(self.titles, seq, "/synthetic/tab5", timeout)
            else:
                title_sync.send_snapshots(
                    self.titles, desktop or self.desktop, seq, "/synthetic/tab5", timeout,
                    ack_timeout=timeout,
                )
        return opened

    def test_combined_transaction_opens_once_and_waits_for_each_correct_sequence_ack(self):
        connection = FakeSerial([
            b"TITLES_OK 36\n", b"HOST_CONFIG_OK 37\n", b'{"native":"event"}\n',
            b"TITLES_OK 37\n", b"HOST_CONFIG_OK 36\n", b"TITLES_OK 37\n",
            b"HOST_CONFIG_OK 37\n",
        ])
        opened = self.send(connection)
        opened.assert_called_once_with("/synthetic/tab5", 0.5)
        self.assertEqual(connection.entered, 1)
        self.assertTrue(connection.closed)
        self.assertIsNone(connection.exit_error)
        self.assertEqual(len(connection.writes), 2)
        first, second = connection.writes
        self.assertTrue(first.startswith(b"TITLES "))
        self.assertTrue(second.startswith(b"HOST_CONFIG "))
        self.assertEqual(json.loads(first.split(b" ", 1)[1])["seq"], 37)
        self.assertEqual(json.loads(second.split(b" ", 1)[1])["seq"], 37)
        self.assertLess(connection.trace.index(("read", b"TITLES_OK 37\n")),
                        connection.trace.index(("write", second)))
        self.assertEqual(sum(event == "flush" for event, _ in connection.trace), 2)

    def test_second_packet_rejection_closes_context_and_reports_failure(self):
        connection = FakeSerial([b"TITLES_OK 37\n", b"HOST_CONFIG_ERROR invalid\n"])
        with self.assertRaisesRegex(RuntimeError, "HOST_CONFIG"):
            self.send(connection)
        self.assertEqual(len(connection.writes), 2)
        self.assertTrue(connection.closed)
        self.assertIs(connection.exit_error, RuntimeError)

    def test_second_packet_timeout_closes_context(self):
        connection = FakeSerial([b"TITLES_OK 37\n", b"HOST_CONFIG_OK 36\n"])
        with self.assertRaisesRegex(TimeoutError, "HOST_CONFIG"):
            self.send(connection, timeout=0.1)
        self.assertEqual(len(connection.writes), 2)
        self.assertTrue(connection.closed)
        self.assertIs(connection.exit_error, TimeoutError)

    def test_title_rejection_or_timeout_still_sends_and_acknowledges_configuration(self):
        class ConfigReplySerial(FakeSerial):
            def write(self, command):
                written = super().write(command)
                if command.startswith(b"HOST_CONFIG "):
                    # A response exists only after its request; a failed title
                    # wait cannot accidentally consume this packet's ACK early.
                    self.lines.extend([b"HOST_CONFIG_OK 36\n", b"HOST_CONFIG_OK 37\n"])
                return written

        for lines, expected_error in (([b"TITLES_ERROR invalid\n"], RuntimeError),
                                      ([b"TITLES_OK 36\n"], TimeoutError)):
            with self.subTest(error=expected_error.__name__):
                connection = ConfigReplySerial(lines)
                with self.assertRaisesRegex(expected_error, "TITLES"):
                    self.send(connection, timeout=0.1)
                self.assertEqual(connection.entered, 1)
                self.assertEqual([command.split(b" ", 1)[0] for command in connection.writes],
                                 [b"TITLES", b"HOST_CONFIG"])
                self.assertIn(("read", b"HOST_CONFIG_OK 37\n"), connection.trace)
                self.assertLess(connection.trace.index(("write", connection.writes[1])),
                                connection.trace.index(("read", b"HOST_CONFIG_OK 37\n")))
                self.assertEqual(sum(event == "flush" for event, _ in connection.trace), 2)
                self.assertTrue(connection.closed)

    def test_both_packets_failing_still_reports_the_first_title_error(self):
        class RejectedConfigSerial(FakeSerial):
            def write(self, command):
                written = super().write(command)
                if command.startswith(b"HOST_CONFIG "):
                    self.lines.append(b"HOST_CONFIG_ERROR invalid\n")
                return written

        connection = RejectedConfigSerial([b"TITLES_ERROR invalid\n"])
        with self.assertRaisesRegex(RuntimeError, "TITLES"):
            self.send(connection)
        self.assertEqual(len(connection.writes), 2)
        self.assertIn(("read", b"HOST_CONFIG_ERROR invalid\n"), connection.trace)
        self.assertTrue(connection.closed)

    def test_invalid_desktop_fails_before_opening_serial(self):
        for snapshot in (DesktopWireTests.snapshot(tier=PRIVATE),
                         DesktopWireTests.snapshot(actions=DEFAULT_ACTIONS[:-1]),
                         DesktopWireTests.snapshot(source="unavailable")):
            with self.subTest(snapshot=snapshot), \
                    mock.patch.object(title_sync, "open_serial") as opened:
                with self.assertRaises(ValueError):
                    title_sync.send_snapshots(self.titles, snapshot, 37)
                opened.assert_not_called()

    def test_legacy_title_only_helper_still_sends_only_the_title_packet(self):
        connection = FakeSerial([b"TITLES_OK 37\n"])
        opened = self.send(connection, title_only=True)
        opened.assert_called_once_with("/synthetic/tab5", 0.5)
        self.assertEqual(len(connection.writes), 1)
        self.assertTrue(connection.writes[0].startswith(b"TITLES "))
        self.assertTrue(connection.closed)


class StopWatch(Exception):
    """End a bounded synthetic watch loop without sleeping or reading devices."""


class DesktopWatchTests(unittest.TestCase):
    def run_watch(self, titles, desktops, *, outcomes=None, start_seq=100):
        self.assertEqual(len(titles), len(desktops))
        clock = {"now": 0.0, "polls": 0}
        sends = []
        output = io.StringIO()
        outcomes = iter(outcomes or ())

        def send(title, desktop, sequence, port, timeout):
            sends.append((clock["now"], title, desktop, sequence, port, timeout))
            outcome = next(outcomes, None)
            if outcome:
                raise outcome

        def sleep(delay):
            self.assertEqual(delay, 1.0)
            clock["polls"] += 1
            clock["now"] += delay
            if clock["polls"] == len(titles):
                raise StopWatch

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(title_sync, "build_snapshot", side_effect=titles))
            stack.enter_context(mock.patch.object(desktop_sync, "build_snapshot", side_effect=desktops))
            stack.enter_context(mock.patch.object(title_sync, "send_snapshots", side_effect=send))
            stack.enter_context(mock.patch.object(title_sync.time, "time", return_value=start_seq))
            stack.enter_context(mock.patch.object(title_sync.time, "monotonic", side_effect=lambda: clock["now"]))
            stack.enter_context(mock.patch.object(title_sync.time, "sleep", side_effect=sleep))
            stack.enter_context(mock.patch.object(title_sync.watch, "_transport_notice", None, create=True))
            stack.enter_context(mock.patch("sys.stdout", output))
            with self.assertRaises(StopWatch):
                title_sync.watch(Path("/synthetic/state"), Path("/synthetic/db"), "/synthetic/tab5")
        return sends, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_unchanged_snapshots_are_resent_at_two_seconds_not_each_poll(self):
        title, desktop = synthetic_titles(), DesktopWireTests.snapshot()
        sends, notices = self.run_watch([title] * 4, [desktop] * 4, start_seq=0xFFFFFFFF)
        self.assertEqual([send[0] for send in sends], [0.0, 2.0])
        self.assertEqual([send[3] for send in sends], [0, 1])
        self.assertTrue(all(send[4:] == ("/synthetic/tab5", 0.5) for send in sends))
        self.assertEqual(len(notices), 1)

    def test_title_or_desktop_change_immediately_resends_both_snapshots(self):
        title, changed_title = synthetic_titles(), synthetic_titles("Changed synthetic title")
        desktop = DesktopWireTests.snapshot()
        changed_desktop = DesktopWireTests.snapshot(tier="ultrafast")
        sends, _ = self.run_watch([title, changed_title, changed_title],
                                 [desktop, desktop, changed_desktop])
        self.assertEqual([send[0] for send in sends], [0.0, 1.0, 2.0])
        self.assertEqual([send[3] for send in sends], [101, 102, 103])
        self.assertEqual([(send[1], send[2]) for send in sends],
                         [(title, desktop), (changed_title, desktop),
                          (changed_title, changed_desktop)])

    def test_failed_transaction_is_retried_next_poll_before_heartbeat(self):
        title, desktop = synthetic_titles(), DesktopWireTests.snapshot()
        sends, _ = self.run_watch([title] * 4, [desktop] * 4,
                                 outcomes=[RuntimeError("Synthetic HOST_CONFIG rejection"), None, None])
        self.assertEqual([send[0] for send in sends], [0.0, 1.0, 3.0])
        self.assertEqual([send[3] for send in sends], [101, 102, 103])
        self.assertTrue(all(send[1:3] == (title, desktop) for send in sends))

    def test_repeated_connection_failures_emit_one_generic_notice_until_recovered(self):
        title, desktop = synthetic_titles(), DesktopWireTests.snapshot()
        failures = [TimeoutError(PRIVATE), TimeoutError(PRIVATE)]
        for error in failures:
            error.packet = "HOST_CONFIG"
            error.failure = "ack-timeout"
            error.failed_packets = (("HOST_CONFIG", "ack-timeout"), (PRIVATE, PRIVATE))
        sends, notices = self.run_watch(
            [title] * 3, [desktop] * 3,
            outcomes=[*failures, None],
        )
        self.assertEqual([send[0] for send in sends], [0.0, 1.0, 2.0])
        transport_notices = [notice for notice in notices if "transport" in notice]
        self.assertEqual(len(transport_notices), 1)
        self.assertEqual(transport_notices[0]["packet"], "HOST_CONFIG")
        self.assertEqual(transport_notices[0]["failure"], "ack-timeout")
        self.assertEqual(transport_notices[0]["failed_packets"],
                         [{"packet": "HOST_CONFIG", "failure": "ack-timeout"}])
        recoveries = [notice for notice in notices if "delivery" in notice]
        self.assertEqual(recoveries, [{"delivery": "acknowledged", "seq": 103,
                                      "packets": ["TITLES", "HOST_CONFIG"],
                                      "failed_attempts": 2}])
        self.assertNotIn(PRIVATE, json.dumps(notices))


class DesktopMainTests(unittest.TestCase):
    def invoke(self, *options, connection=None):
        title = synthetic_titles(PRIVATE)
        desktop = DesktopWireTests.snapshot()
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", ["title_sync.py", *options]))
            stack.enter_context(mock.patch.object(title_sync, "build_snapshot", return_value=title))
            stack.enter_context(mock.patch.object(desktop_sync, "build_snapshot", return_value=desktop))
            opened = stack.enter_context(mock.patch.object(title_sync, "open_serial", return_value=connection))
            stack.enter_context(mock.patch.object(title_sync.time, "time", return_value=123))
            stack.enter_context(mock.patch("sys.stdout", output))
            result = title_sync.main()
        return result, json.loads(output.getvalue()), opened

    def test_default_and_explicit_dry_run_never_open_hardware_or_reveal_titles(self):
        for arguments in ((), ("--dry-run",)):
            with self.subTest(arguments=arguments):
                result, summary, opened = self.invoke(*arguments)
                self.assertEqual(result, 0)
                opened.assert_not_called()
                self.assertEqual(summary["source"], "custom")
                self.assertEqual((summary["configured"], summary["resolved"], summary["empty"]), (1, 1, 5))
                self.assertEqual(summary["desktop"]["tier"], "fast")
                self.assertEqual(summary["desktop"]["actions"], list(DEFAULT_ACTIONS))
                self.assertNotIn("slots", summary)
                self.assertNotIn(PRIVATE, json.dumps(summary))
                self.assertNotIn("local:synthetic-fixed", json.dumps(summary))

    def test_once_opens_one_connection_and_sends_both_versioned_packets(self):
        connection = FakeSerial([b"TITLES_OK 123\n", b"HOST_CONFIG_OK 123\n"])
        result, summary, opened = self.invoke("--once", connection=connection)
        self.assertEqual(result, 0)
        opened.assert_called_once_with(None, 0.5)
        self.assertTrue(connection.closed)
        self.assertEqual([command.split(b" ", 1)[0] for command in connection.writes],
                         [b"TITLES", b"HOST_CONFIG"])
        self.assertEqual([json.loads(command.split(b" ", 1)[1])["seq"]
                          for command in connection.writes], [123, 123])
        self.assertEqual(summary["source"], "custom")
        self.assertIn("desktop", summary)
        self.assertNotIn(PRIVATE, json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
