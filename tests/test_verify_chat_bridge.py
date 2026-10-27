from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import chat_state_bridge as bridge
import verify_chat_bridge as qa

CHAT = "11111111-1111-4111-8111-111111111111"
SECRET = "private-instructions-must-not-be-reported"
COLLABORATION = {"mode": "default", "settings": {
    "model": "test-model", "reasoning_effort": "high",
    "developer_instructions": SECRET}}
BASELINE = {"serviceTier": None, "collaborationMode": COLLABORATION}


def stream(change):
    return {"type": "broadcast", "method": "thread-stream-state-changed",
            "version": 11, "sourceClientId": "owner", "targetClientIds": ["client"],
            "params": {"hostId": "local", "conversationId": CHAT, "change": change}}


def projection():
    value = qa.RawProjection(CHAT)
    value.owner_id, value.client_id = "owner", "client"
    value.consume(stream({"type": "snapshot", "revision": 1,
                          "conversationState": {
                              "latestThreadSettings": deepcopy(BASELINE),
                              "latestCollaborationMode": deepcopy(COLLABORATION),
                              "turns": [{"content": "transcript-must-not-be-retained"}]}}))
    return value


class FakeClient:
    """No sockets or serial: exercise only restore decisions and reports."""
    def __init__(self, *args, **kwargs):
        self.projection = projection()
        self.writes = []
        self.refresh_count = 0
        self.fail_after_write = set()
        self.closed = False
        self.canonical_tiers = False

    def connect(self):
        pass

    def close(self):
        self.closed = True

    def refresh(self, timeout):
        self.refresh_count += 1

    def poll(self):
        return False

    def assign(self, field, value):
        self.projection.raw["latestThreadSettings"][field] = deepcopy(value)
        if field == "collaborationMode":
            self.projection.raw["latestCollaborationMode"] = deepcopy(value)
        self.projection.settings = bridge.project_settings(self.projection.raw)

    def update(self, field, value, timeout):
        self.writes.append((field, deepcopy(value)))
        if field == "serviceTier" and self.canonical_tiers:
            value = "default" if value is None else "fast" if value == "priority" else value
        self.assign(field, value)
        if len(self.writes) in self.fail_after_write:
            raise bridge.BridgeError("Desktop IPC request timed out")


def bounded_visible(*args, **kwargs):
    return {"tier": "standard", "mode": "default", "device_fresh": True,
            "elapsed_ms": 1, "cache_revision": 1}


class RestoreTests(unittest.TestCase):
    def exercise(self, client, field="serviceTier", values=None):
        report = {"checks": []}
        qa.exercise(client, field, values if values is not None else ["priority"],
                    deepcopy(BASELINE), Path("/unused"), None, 6, report)
        return report

    @patch.object(qa, "wait_visible", side_effect=bounded_visible)
    def test_standard_fast_cycle_restores_raw_null(self, _visible):
        client = FakeClient()
        report = self.exercise(client, values=[None, "priority"])
        self.assertEqual(client.writes, [("serviceTier", None),
                                         ("serviceTier", "priority"), ("serviceTier", None)])
        self.assertIsNone(client.projection.field("serviceTier"))
        self.assertTrue(report["checks"][0]["restored"])

    @patch.object(qa, "wait_visible", side_effect=bounded_visible)
    def test_desktop_canonical_null_then_timeout_restores_original_fast(self, _visible):
        client = FakeClient()
        client.assign("serviceTier", "priority")
        client.canonical_tiers = True
        client.fail_after_write = {1}
        baseline = {**deepcopy(BASELINE), "serviceTier": "priority"}
        report = {"checks": []}
        with self.assertRaises(bridge.BridgeError):
            qa.exercise(client, "serviceTier", [None], baseline,
                        Path("/unused"), None, 6, report)
        self.assertEqual(client.writes, [("serviceTier", None), ("serviceTier", "priority")])
        self.assertEqual(client.projection.field("serviceTier"), "fast")
        self.assertTrue(report["checks"][0]["restored"])
        self.assertFalse(report["checks"][0]["conflict"])
        self.assertEqual(report["checks"][0]["original_tier"], {"type": "string", "value": "priority"})

    @patch.object(qa, "wait_visible", side_effect=bounded_visible)
    def test_complete_cycle_accepts_canonical_default_and_fast(self, _visible):
        client = FakeClient()
        client.canonical_tiers = True
        report = self.exercise(client, values=[None, "priority"])
        self.assertEqual(client.projection.field("serviceTier"), "default")
        self.assertTrue(report["checks"][0]["restored"])
        self.assertFalse(report["checks"][0]["conflict"])

    @patch.object(qa, "wait_visible", side_effect=bounded_visible)
    def test_update_timeout_after_acceptance_still_restores(self, _visible):
        client = FakeClient()
        client.fail_after_write = {1}
        report = {"checks": []}
        with self.assertRaises(bridge.BridgeError):
            qa.exercise(client, "serviceTier", ["priority"], deepcopy(BASELINE),
                        Path("/unused"), None, 6, report)
        self.assertEqual(client.writes, [("serviceTier", "priority"), ("serviceTier", None)])
        self.assertTrue(report["checks"][0]["restored"])

    def test_device_wait_failure_restores_before_reporting_failure(self):
        client = FakeClient()
        report = {"checks": []}
        with patch.object(qa, "wait_visible", side_effect=[qa.QAError("Synthetic device wait failed"), {}]):
            with self.assertRaises(qa.QAError):
                qa.exercise(client, "serviceTier", ["priority"], deepcopy(BASELINE),
                            Path("/unused"), None, 6, report)
        self.assertIsNone(client.projection.field("serviceTier"))
        self.assertTrue(report["checks"][0]["restored"])

    def test_observed_user_change_is_preserved_during_failure(self):
        client = FakeClient()
        report = {"checks": []}
        def changed(*_args, **_kwargs):
            client.assign("serviceTier", "ultrafast")
            raise qa.QAError("Synthetic concurrent change")
        with patch.object(qa, "wait_visible", side_effect=changed), self.assertRaises(qa.QAError):
            qa.exercise(client, "serviceTier", ["priority"], deepcopy(BASELINE),
                        Path("/unused"), None, 6, report)
        self.assertEqual(client.writes, [("serviceTier", "priority")])
        self.assertEqual(client.projection.field("serviceTier"), "ultrafast")
        self.assertTrue(report["checks"][0]["conflict"])
        self.assertFalse(report["checks"][0]["restored"])

    def test_user_change_before_first_write_is_not_overwritten(self):
        client = FakeClient()
        client.assign("serviceTier", "ultrafast")
        with patch.object(qa, "wait_visible"), self.assertRaises(qa.QAError):
            self.exercise(client)
        self.assertEqual(client.writes, [])

    @patch.object(qa, "wait_visible", side_effect=bounded_visible)
    def test_full_collaboration_object_restored_without_reporting_instructions(self, _visible):
        client = FakeClient()
        alternate = deepcopy(COLLABORATION)
        alternate["mode"] = "plan"
        baseline_before = deepcopy(BASELINE)
        report = self.exercise(client, "collaborationMode", [alternate])
        self.assertEqual(client.writes[-1], ("collaborationMode", COLLABORATION))
        self.assertEqual(client.projection.field("collaborationMode"), COLLABORATION)
        self.assertEqual(BASELINE, baseline_before)
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn("developer_instructions", json.dumps(report))

    def test_restore_failure_is_explicit_and_never_claims_restored(self):
        client = FakeClient()
        client.fail_after_write = {2}
        report = {"checks": []}
        with patch.object(qa, "wait_visible", side_effect=bounded_visible), self.assertRaises(bridge.BridgeError):
            qa.exercise(client, "serviceTier", ["priority"], deepcopy(BASELINE),
                        Path("/unused"), None, 6, report)
        self.assertFalse(report["checks"][0]["restored"])
        self.assertEqual(report["checks"][0]["restore_error"], "BridgeError")


class BaselineAndReportTests(unittest.TestCase):
    def test_visible_wait_accepts_canonical_default_without_false_timeout(self):
        client = FakeClient()
        client.assign("serviceTier", "default")
        cache = client.projection.metadata()
        device = {"status": {"lighting_preview": False, "host_feedback": {"chat": {
            "fresh": True, "source": "desktop-ipc", "scope": "explicit-target",
            "thread_id": CHAT, "speed": "standard", "mode": "default"}}}}
        with patch.object(qa, "read_cache", return_value=cache), \
                patch.object(qa, "device_status", return_value=device) as read_status:
            result = qa.wait_visible(client, {"tier": "standard", "mode": "default"},
                                     Path("/unused"), None, 3, "serviceTier", None)
        read_status.assert_called_once()
        self.assertEqual(result["observed_tier"], {"type": "string", "value": "default"})

    def test_tier_equivalence_is_narrow_and_mode_remains_exact(self):
        self.assertTrue(qa.same_field("serviceTier", None, "default"))
        self.assertTrue(qa.same_field("serviceTier", "fast", "priority"))
        for left, right in ((None, "standard"), (False, None), ("priority", "ultrafast"),
                            ({}, {}), (qa.MISSING, None)):
            with self.subTest(left=left, right=right):
                self.assertFalse(qa.same_field("serviceTier", left, right))
        changed = deepcopy(COLLABORATION)
        changed["settings"]["developer_instructions"] = "changed"
        self.assertFalse(qa.same_field("collaborationMode", COLLABORATION, changed))
        self.assertEqual(qa.bounded_tier(SECRET), {"type": "string", "value": "other"})

    def test_missing_each_required_raw_field_rejects_mutation(self):
        for field in ("serviceTier", "collaborationMode"):
            client = FakeClient()
            if field == "serviceTier":
                del client.projection.raw["latestThreadSettings"][field]
            else:
                client.projection.raw.pop("latestCollaborationMode")
                del client.projection.raw["latestThreadSettings"][field]
            with self.subTest(field=field), self.assertRaises(qa.QAError):
                qa.validate_baseline(client)
            self.assertEqual(client.writes, [])

    def test_baseline_is_independent_copy_and_transcript_is_discarded(self):
        client = FakeClient()
        raw = qa.validate_baseline(client)
        raw["collaborationMode"]["settings"]["model"] = "changed"
        self.assertEqual(client.projection.field("collaborationMode"), COLLABORATION)
        self.assertNotIn("transcript-must-not-be-retained", repr(client.projection.raw))

    def test_raw_collaboration_patch_and_invalidation(self):
        value = projection()
        value.consume(stream({"type": "patches", "baseRevision": 1, "revision": 2,
                              "patches": [{"op": "replace", "path": ["latestCollaborationMode", "mode"],
                                           "value": "plan"}]}))
        self.assertEqual(value.field("collaborationMode")["mode"], "plan")
        self.assertEqual(value.field("collaborationMode")["settings"], COLLABORATION["settings"])
        value.invalidate()
        self.assertEqual(value.raw, {})

    def test_readonly_verify_report_does_not_include_raw_settings(self):
        client = FakeClient()
        args = SimpleNamespace(thread_id=CHAT, mutate=False, socket=Path("/unused"),
                               timeout=6, live_cache=Path("/unused"), port=None, fast_tier="priority")
        with patch.object(qa, "QAClient", return_value=client), \
                patch.object(qa, "wait_visible", side_effect=bounded_visible):
            report = qa.verify(args)
        self.assertTrue(report["success"])
        self.assertTrue(client.closed)
        self.assertEqual(client.writes, [])
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn("developer_instructions", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
