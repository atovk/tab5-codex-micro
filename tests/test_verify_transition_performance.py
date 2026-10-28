from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import verify_transition_performance as qa


class FakeSession:
    """Model ACKs and diagnostic contamination without any serial access."""
    def __init__(self, *, version="0.11.1", playing=False, settings=True):
        self.events = []
        self.trace = []
        self.fail_command = None
        self.fail_after_accept = False
        self.failed = False
        self.frame_failure = False
        self.pending_reads = 0
        self.limit_us = 1_200_000
        self.current = {
            "firmware": f"tab5-micro-{version}", "theme": "light", "language": "en",
            "settings_open": settings, "playing": playing, "settings_pending": False,
            "input_capture": False, "lighting_preview": False, "effects_frozen": False,
            "inputs_tx": 42, "captured_inputs": 9,
            "ui_profile": {"full_frames": 1, "full_total_us": 1_100_000},
            "title_scroll": [{"ready": True} for _ in range(7)],
            "host_feedback": {"chat": {"thread_id": "private-id", "title": "private-title"}},
        }
        self.current.update({key: 1 for key in qa.METRICS})
        self.original_effects = self.current["effects_frozen"]

    def status(self):
        self.trace.append("STATUS")
        if self.pending_reads:
            self.current["settings_pending"] = True
            self.pending_reads -= 1
        else:
            self.current["settings_pending"] = False
        result = deepcopy(self.current)
        # The response's own write only affects future samples; PERF_RESET
        # must remove it before every phase.
        self.current["input_interval_max_us"] = 9_000_000
        return result

    def command(self, value, expected):
        self.trace.append(value)
        fail = value == self.fail_command and not self.failed
        if fail and not self.fail_after_accept:
            self.failed = True
            raise TimeoutError("Synthetic ACK timeout")
        if value == "INPUT_TEST_BEGIN":
            if self.current.get("playing"):
                raise RuntimeError("INPUT_TEST_ERROR")
            self.original_effects = self.current["effects_frozen"]
            self.current.update(input_capture=True, effects_frozen=True)
        elif value == "INPUT_TEST_END":
            self.current.update(input_capture=False, lighting_preview=False,
                                effects_frozen=self.original_effects)
        elif value == "FX FREEZE":
            self.current["effects_frozen"] = True
        elif value == "PERF_RESET":
            self.current.update({key: 0 for key in qa.METRICS})
        elif value.startswith("THEME "):
            self.current["theme"] = value.split()[1].lower()
        elif value.startswith("LANGUAGE "):
            self.current["language"] = value.split()[1].lower()
        elif value.startswith("SETTINGS "):
            self.current["settings_open"] = value.endswith("OPEN")
        elif value in ("PLAY", "MICRO"):
            self.current["settings_open"] = False
            if "playing" in self.current:
                self.current["playing"] = value == "PLAY"
        elif value.startswith(("PREVIEW_LIGHTS ", "PREVIEW_HOST ")):
            self.current["lighting_preview"] = True
        elif value == "PREVIEW_LIGHTS_CLEAR":
            self.current["lighting_preview"] = False
        else:
            raise AssertionError(f"Unexpected command: {value}")
        if value != "PERF_RESET":
            self.current.update(render_samples=1, input_interval_samples=20,
                                render_last_us=1_100_000, render_max_us=1_100_000,
                                input_interval_last_us=2_000,
                                input_interval_max_us=self.limit_us)
        if fail:
            self.failed = True
            raise TimeoutError("Synthetic ACK timeout after acceptance")

    def recover_frame(self):
        self.trace.append("RECOVER_FRAME")
        if self.frame_failure:
            raise RuntimeError("Synthetic binary stream is incomplete")


class PhaseTests(unittest.TestCase):
    def test_phase_isolates_previous_diagnostics_and_samples_after_quiet_settle(self):
        session = FakeSession(settings=False)
        baseline = deepcopy(session.current)
        session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
        session.trace.clear()
        with patch.object(qa, "quiet", side_effect=lambda value: session.trace.append(("quiet", value))):
            result = qa.run_phase(session, baseline, "theme_dark", "THEME DARK",
                                  {"theme": "dark"}, 2)
        self.assertEqual(session.trace, ["FX FREEZE", "STATUS", ("quiet", .25),
                                        "PERF_RESET", "THEME DARK", ("quiet", 2), "STATUS"])
        self.assertEqual(result["input_interval_max_us"], session.limit_us)
        self.assertEqual(result["ui_profile"]["full_total_us"], 1_100_000)
        self.assertNotIn("private", json.dumps(result))

    def test_missing_completed_render_is_not_accepted(self):
        session = FakeSession(settings=False)
        baseline = deepcopy(session.current)
        session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
        original = session.command

        def command(value, expected):
            original(value, expected)
            if value == "THEME DARK":
                session.current["render_samples"] = 0

        with patch.object(session, "command", side_effect=command), patch.object(qa, "quiet"), \
                self.assertRaisesRegex(RuntimeError, "no completed redraw"):
            qa.run_phase(session, baseline, "theme_dark", "THEME DARK", {"theme": "dark"}, 2)

    def test_fixture_titles_fit_protocol_and_are_longer_than_two_lines(self):
        self.assertEqual(len(set(qa.LONG_TITLES)), 6)
        for title in (*qa.LONG_TITLES, qa.LONG_TRACKED_TITLE):
            self.assertLessEqual(len(title.encode("utf-8")), 191)
            self.assertGreater(len(title), 40)
        qa.preview_fixtures(qa.LONG_TITLES)


class VerifyTests(unittest.TestCase):
    def exercise(self, session, *, expected_error=None, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "transition.json"
            with patch.object(qa, "open_serial", return_value=nullcontext(object())) as opened, \
                    patch.object(qa, "Session", return_value=session), patch.object(qa, "quiet"):
                if expected_error:
                    with self.assertRaises(expected_error):
                        qa.verify(output, **kwargs)
                    result = None
                else:
                    result = qa.verify(output, **kwargs)
                opened.assert_called_once()
            return result, json.loads(output.read_text()) if output.exists() else None

    def assert_restored(self, session, report, *, settings=True, playing=False):
        self.assertFalse(session.current["input_capture"])
        self.assertFalse(session.current["lighting_preview"])
        self.assertFalse(session.current["effects_frozen"])
        self.assertEqual(session.current["theme"], "light")
        self.assertEqual(session.current["language"], "en")
        self.assertEqual(session.current["settings_open"], settings)
        self.assertEqual(session.current.get("playing", False), playing)
        self.assertFalse(report["after"]["settings_pending"])
        self.assertEqual(report["after"]["inputs_tx"], 42)
        self.assertNotIn("cleanup_errors", report)
        self.assertNotIn("SCREEN", session.trace)
        self.assertFalse(any(command.startswith(("TITLES ", "HOST_CONFIG "))
                             for command in session.trace))
        self.assertNotIn("private", json.dumps(report))

    def test_slow_baseline_reports_every_phase_without_implicit_threshold(self):
        session = FakeSession()
        result, report = self.exercise(session)
        self.assertTrue(result["success"])
        self.assertEqual(len(report["phases"]), 10)
        self.assertEqual(session.trace.count("PERF_RESET"), 10)
        self.assertTrue(all(phase["within_input_limit"] is None for phase in report["phases"]))
        self.assertEqual(report["phases"][-2]["commands"], ["PREVIEW_LIGHTS", "PREVIEW_HOST"])
        self.assert_restored(session, report)

    def test_explicit_limit_collects_all_phases_before_failure_and_restores(self):
        session = FakeSession()
        _, report = self.exercise(session, expected_error=RuntimeError, max_input_us=50_000)
        self.assertFalse(report["success"])
        self.assertEqual(len(report["phases"]), 10)
        self.assertTrue(all(phase["within_input_limit"] is False for phase in report["phases"]))
        self.assert_restored(session, report)

    def test_original_play_page_is_restored(self):
        session = FakeSession(playing=True, settings=False)
        _, report = self.exercise(session)
        self.assertLess(session.trace.index("MICRO"), session.trace.index("INPUT_TEST_BEGIN"))
        self.assert_restored(session, report, settings=False, playing=True)

    def test_legacy_firmware_page_is_inferred_only_after_begin_acceptance(self):
        session = FakeSession(version="0.11.0")
        del session.current["playing"]
        _, report = self.exercise(session, firmware="0.11.0")
        self.assertIn("legacy_page_evidence", report)
        self.assertLess(session.trace.index("INPUT_TEST_BEGIN"), session.trace.index("MICRO"))
        self.assert_restored(session, report)

    def test_accepted_begin_with_lost_ack_is_cleaned_up(self):
        session = FakeSession()
        session.fail_command = "INPUT_TEST_BEGIN"
        session.fail_after_accept = True
        _, report = self.exercise(session, expected_error=RuntimeError)
        self.assertIn("INPUT_TEST_END", session.trace)
        self.assert_restored(session, report)

    def test_rejected_begin_does_not_end_real_input_or_mutate_preferences(self):
        session = FakeSession()
        session.fail_command = "INPUT_TEST_BEGIN"
        _, report = self.exercise(session, expected_error=RuntimeError)
        self.assertNotIn("INPUT_TEST_END", session.trace)
        self.assertFalse(any(value.startswith(("THEME ", "LANGUAGE ")) for value in session.trace))
        self.assert_restored(session, report)

    def test_existing_capture_is_never_claimed_or_ended(self):
        session = FakeSession()
        session.current["input_capture"] = True
        _, report = self.exercise(session, expected_error=RuntimeError)
        self.assertIsNone(report)
        self.assertEqual(session.trace, ["STATUS"])
        self.assertTrue(session.current["input_capture"])

    def test_frame_recovery_failure_prevents_text_cleanup(self):
        session = FakeSession()
        session.fail_command = "THEME DARK"
        session.frame_failure = True
        _, report = self.exercise(session, expected_error=RuntimeError)
        self.assertEqual(session.trace[-1], "RECOVER_FRAME")
        self.assertIn("watchdog", report["cleanup_errors"][-1])
        self.assertNotIn("after", report)

    def test_native_counter_change_fails(self):
        session = FakeSession()
        original = session.command

        def command(value, expected):
            original(value, expected)
            if value == "LANGUAGE EN":
                session.current["inputs_tx"] += 1

        with patch.object(session, "command", side_effect=command):
            _, report = self.exercise(session, expected_error=RuntimeError)
        self.assertIn("Native host input counter changed", report["error"])
        self.assertFalse(report["success"])

    def test_pending_preferences_are_polled_until_saved(self):
        session = FakeSession()
        original = session.command

        def command(value, expected):
            original(value, expected)
            if value == "INPUT_TEST_END":
                session.pending_reads = 3

        with patch.object(session, "command", side_effect=command):
            _, report = self.exercise(session)
        self.assertEqual(session.trace[-4:], ["STATUS"] * 4)
        self.assert_restored(session, report)

    def test_exact_firmware_validation(self):
        self.assertEqual(qa.firmware_name("0.11.1"), "tab5-micro-0.11.1")
        self.assertEqual(qa.firmware_name("tab5-micro-0.11.0"), "tab5-micro-0.11.0")
        for value in ("0.11", "v0.11.1", "0.11.1-extra", "0.11.1\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qa.firmware_name(value)


if __name__ == "__main__":
    unittest.main()
