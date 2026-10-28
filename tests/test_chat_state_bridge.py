from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import chat_state_bridge as bridge

CHAT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
OWNER, CLIENT = "owner-1", "bridge-1"
SECRET = "private-prompt-must-not-leave-memory"


def stream(change, **overrides):
    envelope = {"type": "broadcast", "method": "thread-stream-state-changed",
                "version": 11, "sourceClientId": OWNER, "targetClientIds": [CLIENT],
                "params": {"hostId": "local", "conversationId": CHAT, "change": change}}
    envelope.update(overrides)
    return envelope


def snapshot(revision=3, tier="priority", mode="default"):
    return stream({"type": "snapshot", "revision": revision, "conversationState": {
        "latestThreadSettings": {"serviceTier": tier, "model": "gpt-6.1-sol", "secret": SECRET},
        "latestCollaborationMode": {"mode": mode, "settings": {"developer_instructions": SECRET}},
        "turns": [{"content": SECRET}], "title": SECRET}})


def patches(values, base=3, revision=4):
    return stream({"type": "patches", "baseRevision": base, "revision": revision, "patches": values})


def projection():
    value = bridge.MetadataProjection(CHAT)
    value.owner_id, value.client_id = OWNER, CLIENT
    return value


class FramingTests(unittest.TestCase):
    def test_every_byte_fragment_and_unicode(self):
        message = {"field": "计划模式"}
        reader = bridge.FrameReader()
        result = []
        for byte in bridge.encode_frame(message):
            result.extend(reader.feed(bytes([byte])))
        self.assertEqual(result, [message])
        self.assertEqual(reader.buffer, b"")
        self.assertIsNone(reader.expected)

    def test_two_frames_and_partial_next_header(self):
        first, second = {"x": 1}, {"x": 2}
        encoded = bridge.encode_frame(first) + bridge.encode_frame(second)
        split = len(bridge.encode_frame(first)) + 2
        reader = bridge.FrameReader()
        self.assertEqual(reader.feed(encoded[:split]), [first])
        self.assertEqual(reader.feed(encoded[split:]), [second])

    def test_header_is_utf8_byte_length(self):
        frame = bridge.encode_frame({"text": "模式"})
        self.assertEqual(struct.unpack("<I", frame[:4])[0], len(frame[4:]))

    def test_invalid_lengths_fail_before_payload_allocation(self):
        for size in (0, bridge.MAX_FRAME_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(size=size), self.assertRaises(bridge.BridgeError):
                bridge.FrameReader().feed(struct.pack("<I", size))

    def test_invalid_json_utf8_and_nonobject(self):
        for payload in (b"{", b"\xff", b"null", b"[]", b"42"):
            with self.subTest(payload=payload), self.assertRaises(bridge.BridgeError):
                bridge.FrameReader().feed(struct.pack("<I", len(payload)) + payload)


class ProjectionTests(unittest.TestCase):
    def test_snapshot_discards_content_and_retains_real_modes(self):
        value = projection()
        self.assertTrue(value.consume(snapshot(mode="plan"), now=123.0))
        metadata = value.metadata()
        self.assertEqual((metadata["tier"], metadata["mode"], metadata["revision"]), ("fast", "plan", 3))
        self.assertEqual(metadata["observed_at"], 123.0)
        self.assertEqual(metadata["scope"], "explicit-target")
        self.assertEqual(metadata["model"], "gpt-6.1-sol")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_explicit_null_and_default_are_standard_but_missing_is_unknown(self):
        for tier in (None, "default"):
            value = projection()
            value.consume(snapshot(tier=tier))
            self.assertEqual(value.metadata()["tier"], "standard")
        event = snapshot()
        del event["params"]["change"]["conversationState"]["latestThreadSettings"]["serviceTier"]
        value.consume(event)
        self.assertEqual(value.metadata()["tier"], "unknown")

    def test_supported_and_unknown_tiers(self):
        for raw, expected in (("fast", "fast"), ("priority", "fast"), ("ultrafast", "ultrafast"),
                              ("standard", "standard"), ("flex", "unknown"), (True, "unknown"),
                              ({"content": SECRET}, "unknown")):
            value = projection()
            value.consume(snapshot(tier=raw))
            self.assertEqual(value.metadata()["tier"], expected)
            self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_settings_collaboration_fallback_only_when_direct_field_missing(self):
        event = snapshot()
        state = event["params"]["change"]["conversationState"]
        del state["latestCollaborationMode"]
        state["latestThreadSettings"]["collaborationMode"] = {"mode": "plan", "secret": SECRET}
        value = projection()
        value.consume(event)
        self.assertEqual(value.metadata()["mode"], "plan")
        state["latestCollaborationMode"] = None
        value.consume(event)
        self.assertEqual(value.metadata()["mode"], "unknown")

    def test_patch_can_switch_fast_off_and_plan_on(self):
        value = projection()
        value.consume(snapshot())
        value.consume(patches([
            {"op": "replace", "path": ["latestThreadSettings", "serviceTier"], "value": None},
            {"op": "replace", "path": ["latestCollaborationMode", "mode"], "value": "plan"},
        ]))
        self.assertEqual((value.metadata()["tier"], value.metadata()["mode"]), ("standard", "plan"))

    def test_unrelated_patches_advance_revision_without_retaining_values(self):
        value = projection()
        value.consume(snapshot())
        value.consume(patches([{"op": "add", "path": ["turns", 0, "content"], "value": SECRET}]))
        self.assertEqual(value.revision, 4)
        self.assertEqual(value.metadata()["tier"], "fast")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_parent_replacement_and_removal_clear_obsolete_fields(self):
        value = projection()
        value.consume(snapshot())
        value.consume(patches([
            {"op": "replace", "path": ["latestThreadSettings"], "value": {"model": "gpt-6-sol", "private": SECRET}},
            {"op": "remove", "path": ["latestCollaborationMode"]},
        ]))
        self.assertEqual(value.metadata()["tier"], "unknown")
        self.assertEqual(value.metadata()["mode"], "unknown")
        self.assertEqual(value.metadata()["model"], "gpt-6-sol")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_root_replacement_projects_whitelist(self):
        value = projection()
        value.consume(snapshot())
        value.consume(patches([{"op": "replace", "path": [], "value": {
            "latestThreadSettings": {"serviceTier": "ultrafast"}, "turns": [SECRET]}}]))
        self.assertEqual(value.metadata()["tier"], "ultrafast")
        self.assertEqual(value.metadata()["mode"], "unknown")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_nested_collaboration_patch_and_remove(self):
        value = projection()
        event = snapshot()
        del event["params"]["change"]["conversationState"]["latestCollaborationMode"]
        value.consume(event)
        value.consume(patches([{"op": "add", "path": ["latestThreadSettings", "collaborationMode", "mode"], "value": "plan"}]))
        self.assertEqual(value.metadata()["mode"], "plan")
        value.consume(patches([{"op": "remove", "path": ["latestThreadSettings", "collaborationMode", "mode"]}], base=4, revision=5))
        self.assertEqual(value.metadata()["mode"], "unknown")

    def test_revision_gap_invalidates_before_resync(self):
        value = projection()
        value.consume(snapshot())
        with self.assertRaises(bridge.RevisionGap):
            value.consume(patches([], base=5, revision=6))
        self.assertEqual(value.metadata()["connection"], "waiting")
        self.assertEqual(value.metadata()["tier"], "unknown")
        self.assertEqual(value.metadata()["observed_at"], 0)
        value.consume(snapshot(revision=7, mode="plan"))
        self.assertEqual(value.metadata()["mode"], "plan")

    def test_patch_before_snapshot_invalidates(self):
        with self.assertRaises(bridge.RevisionGap):
            projection().consume(patches([]))

    def test_resync_rejects_snapshot_older_than_last_observed_revision(self):
        value = projection()
        value.consume(snapshot(revision=8))
        with self.assertRaises(bridge.RevisionGap):
            value.consume(patches([], base=3, revision=4))
        self.assertFalse(value.consume(snapshot(revision=7)))
        with self.assertRaises(bridge.RevisionGap):
            value.consume(patches([], base=8, revision=9))
        self.assertFalse(value.consume(snapshot(revision=8)))
        self.assertTrue(value.consume(snapshot(revision=9)))

    def test_older_snapshot_cannot_overwrite_newer(self):
        value = projection()
        value.consume(snapshot(revision=8))
        self.assertFalse(value.consume(snapshot(revision=7, mode="plan")))
        self.assertEqual(value.metadata()["mode"], "default")

    def test_wrong_identity_host_owner_version_or_target_is_ignored(self):
        mutations = [lambda e: e.update(sourceClientId="other"), lambda e: e.update(version=10),
                     lambda e: e.update(targetClientIds=["other"]),
                     lambda e: e["params"].update(hostId="remote"),
                     lambda e: e["params"].update(conversationId=OTHER)]
        for mutate in mutations:
            event, value = snapshot(), projection()
            mutate(event)
            self.assertFalse(value.consume(event))
            self.assertEqual(value.metadata()["connection"], "waiting")

    def test_uninitialized_identity_cannot_accept_anonymous_snapshot(self):
        value = bridge.MetadataProjection(CHAT)
        event = snapshot()
        event["sourceClientId"] = None
        del event["targetClientIds"]
        self.assertFalse(value.consume(event))

    def test_invalid_patch_batch_is_atomic(self):
        value = projection()
        value.consume(snapshot())
        before = deepcopy(value.__dict__)
        with self.assertRaises(bridge.BridgeError):
            value.consume(patches([
                {"op": "replace", "path": ["latestThreadSettings", "serviceTier"], "value": None},
                {"op": "arbitrary", "path": []},
            ]))
        self.assertEqual(value.__dict__, before)

    def test_invalid_revisions_are_rejected(self):
        for revision in (True, -1, 1.25, "4", None, bridge.MAX_REVISION + 1):
            event = snapshot()
            event["params"]["change"]["revision"] = revision
            with self.subTest(revision=revision), self.assertRaises(bridge.BridgeError):
                projection().consume(event)

    def test_disconnect_clears_state_even_with_heartbeat_argument(self):
        value = projection()
        value.consume(snapshot())
        value.invalidate("disconnected")
        data = value.metadata(heartbeat=9999)
        self.assertEqual((data["tier"], data["mode"], data["observed_at"], data["revision"]), ("unknown", "unknown", 0, 0))

    def test_unsupported_ids_and_remote_hosts_are_rejected(self):
        for value in ("latest", "", "../../../config", None):
            with self.assertRaises(ValueError):
                bridge.MetadataProjection(value)
        with self.assertRaises(ValueError):
            bridge.MetadataProjection(CHAT, "remote")


class PermissionTests(unittest.TestCase):
    def event(self, approval="never", sandbox=None, reviewer="user", profile=None):
        event = snapshot()
        state = event["params"]["change"]["conversationState"]["latestThreadSettings"]
        state.update(approvalPolicy=approval, approvalsReviewer=reviewer,
                     sandboxPolicy=sandbox or {"type": "dangerFullAccess"},
                     activePermissionProfile=profile)
        return event

    def test_actual_full_access_builtin_and_legacy(self):
        for profile in (None, {"id": ":danger-full-access", "extends": None}):
            value = projection()
            value.consume(self.event(profile=profile))
            self.assertEqual(value.metadata()["permission"], "full-access")

    def test_never_alone_cannot_imply_full_access(self):
        value = projection()
        value.consume(self.event(sandbox={"type": "workspaceWrite", "networkAccess": False,
                                          "excludeSlashTmp": False, "excludeTmpdirEnvVar": False}))
        self.assertEqual(value.metadata()["permission"], "custom")

    def test_custom_profile_takes_precedence_and_its_name_does_not_leak(self):
        value = projection()
        value.consume(self.event(profile={"id": SECRET, "extends": SECRET}))
        self.assertEqual(value.metadata()["permission"], "custom")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_builtin_full_access_requires_matching_reviewer_and_policy(self):
        for approval, reviewer in (("on-request", "user"), ("never", "auto_review"), ("never", "guardian_subagent")):
            value = projection()
            value.consume(self.event(approval=approval, reviewer=reviewer, profile={"id": ":danger-full-access"}))
            self.assertEqual(value.metadata()["permission"], "custom")

    def test_missing_any_effective_permission_field_is_unknown(self):
        for field in ("approvalPolicy", "approvalsReviewer", "sandboxPolicy"):
            event = self.event()
            del event["params"]["change"]["conversationState"]["latestThreadSettings"][field]
            value = projection()
            value.consume(event)
            self.assertEqual(value.metadata()["permission"], "unknown")

    def test_readonly_and_workspace_presets(self):
        cases = [({"type": "readOnly", "networkAccess": False}, ":read-only", "read-only"),
                 ({"type": "workspaceWrite", "networkAccess": False, "excludeSlashTmp": False,
                   "excludeTmpdirEnvVar": False}, ":workspace", "workspace-write")]
        for sandbox, identity, expected in cases:
            for profile in (None, {"id": identity}):
                value = projection()
                value.consume(self.event(approval="on-request", sandbox=sandbox, profile=profile))
                self.assertEqual(value.metadata()["permission"], expected)

    def test_noncanonical_legacy_sandbox_is_custom(self):
        for sandbox in ({"type": "externalSandbox", "networkAccess": "enabled"},
                        {"type": "readOnly", "networkAccess": True},
                        {"type": "workspaceWrite", "networkAccess": True, "excludeSlashTmp": False,
                         "excludeTmpdirEnvVar": False}):
            value = projection()
            value.consume(self.event(approval="on-request", sandbox=sandbox))
            self.assertEqual(value.metadata()["permission"], "custom")

    def test_paths_and_extra_policy_text_are_not_retained(self):
        value = projection()
        value.consume(self.event(approval={"granular": {"private": SECRET}}, sandbox={
            "type": "workspaceWrite", "networkAccess": False, "excludeSlashTmp": False,
            "excludeTmpdirEnvVar": False, "writableRoots": [SECRET], "private": SECRET}))
        self.assertEqual(value.metadata()["permission"], "custom")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_canonical_granular_and_guardian_workspace(self):
        for approval, reviewer in ((bridge.GRANULAR_PRESET, "user"), ("on-request", "guardian_subagent"),
                                   ("on-request", "auto_review")):
            value = projection()
            value.consume(self.event(approval=approval, reviewer=reviewer,
                sandbox={"type": "workspaceWrite", "networkAccess": False,
                         "excludeSlashTmp": False, "excludeTmpdirEnvVar": False}, profile={"id": ":workspace"}))
            self.assertEqual(value.metadata()["permission"], "workspace-write")

    def test_permission_patches_follow_actual_changes_and_removals(self):
        value = projection()
        value.consume(self.event(profile={"id": ":danger-full-access"}))
        value.consume(patches([
            {"op": "replace", "path": ["latestThreadSettings", "activePermissionProfile", "id"], "value": ":workspace"},
            {"op": "replace", "path": ["latestThreadSettings", "approvalPolicy"], "value": "on-request"},
            {"op": "replace", "path": ["latestThreadSettings", "sandboxPolicy"], "value": {
                "type": "workspaceWrite", "networkAccess": False, "excludeSlashTmp": False,
                "excludeTmpdirEnvVar": False, "writableRoots": [SECRET]}},
        ]))
        self.assertEqual(value.metadata()["permission"], "workspace-write")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))
        value.consume(patches([{"op": "remove", "path": ["latestThreadSettings", "sandboxPolicy"]}], base=4, revision=5))
        self.assertEqual(value.metadata()["permission"], "unknown")

    def test_nested_sandbox_patch_changes_legacy_preset(self):
        value = projection()
        value.consume(self.event(approval="on-request", sandbox={"type": "readOnly", "networkAccess": False}))
        value.consume(patches([{"op": "replace", "path": ["latestThreadSettings", "sandboxPolicy", "networkAccess"], "value": True}]))
        self.assertEqual(value.metadata()["permission"], "custom")

    def test_nested_granular_patch_is_bounded_and_not_falsely_preset(self):
        value = projection()
        value.consume(self.event(approval=deepcopy(bridge.GRANULAR_PRESET), profile={"id": ":workspace"}))
        value.consume(patches([{"op": "replace", "path": ["latestThreadSettings", "approvalPolicy", "granular", "rules"], "value": SECRET}]))
        self.assertEqual(value.metadata()["permission"], "custom")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_unknown_approval_patch_invalidates_until_matching_full_snapshot(self):
        for path in (["future_policy"], ["granular", "future_flag"],
                     ["granular", "rules", "future_flag"]):
            with self.subTest(path=path):
                value = projection()
                event = self.event(approval=deepcopy(bridge.GRANULAR_PRESET), profile={"id": ":workspace"})
                value.consume(event)
                self.assertEqual(value.metadata()["permission"], "workspace-write")
                with self.assertRaises(bridge.SnapshotRequired):
                    value.consume(patches([
                        {"op": "replace", "path": ["latestThreadSettings", "serviceTier"], "value": None},
                        {"op": "add", "path": ["latestThreadSettings", "approvalPolicy", *path], "value": SECRET},
                    ]))
                self.assertEqual(value.settings, {})
                self.assertEqual((value.metadata()["permission"], value.metadata()["tier"]), ("unknown", "unknown"))
                self.assertEqual(value.metadata()["connection"], "waiting")
                self.assertNotIn(SECRET, json.dumps(value.__dict__))
                # A previously queued snapshot must not restore the old preset.
                self.assertFalse(value.consume(event))
                event["params"]["change"]["revision"] = 4
                approval = event["params"]["change"]["conversationState"]["latestThreadSettings"]["approvalPolicy"]
                target = approval
                for key in path[:-1]:
                    if not isinstance(target.get(key), dict):
                        target[key] = {}
                    target = target[key]
                target[path[-1]] = SECRET
                self.assertTrue(value.consume(event))
                self.assertEqual(value.metadata()["permission"], "custom")
                self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_unknown_approval_removal_resnapshots_instead_of_retaining_custom(self):
        approval = deepcopy(bridge.GRANULAR_PRESET)
        approval["granular"]["future_flag"] = True
        value = projection()
        value.consume(self.event(approval=approval, profile={"id": ":workspace"}))
        self.assertEqual(value.metadata()["permission"], "custom")
        with self.assertRaises(bridge.SnapshotRequired):
            value.consume(patches([{"op": "remove", "path": ["latestThreadSettings", "approvalPolicy", "granular", "future_flag"]}]))
        self.assertEqual(value.metadata()["permission"], "unknown")
        event = self.event(approval=deepcopy(bridge.GRANULAR_PRESET), profile={"id": ":workspace"})
        event["params"]["change"]["revision"] = 4
        value.consume(event)
        self.assertEqual(value.metadata()["permission"], "workspace-write")

    def test_known_patch_after_lossy_approval_projection_requires_snapshot(self):
        approval = deepcopy(bridge.GRANULAR_PRESET)
        approval["granular"]["rules"] = SECRET
        value = projection()
        value.consume(self.event(approval=approval, profile={"id": ":workspace"}))
        with self.assertRaises(bridge.SnapshotRequired):
            value.consume(patches([{"op": "replace", "path": ["latestThreadSettings", "approvalPolicy", "granular", "rules"], "value": False}]))
        self.assertEqual(value.metadata()["permission"], "unknown")
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_ignored_sandbox_paths_do_not_force_snapshot_or_retain_paths(self):
        value = projection()
        value.consume(self.event())
        value.consume(patches([
            {"op": "add", "path": ["latestThreadSettings", "sandboxPolicy", "writableRoots", 0], "value": SECRET},
            {"op": "replace", "path": ["latestThreadSettings", "activePermissionProfile", "displayName"], "value": SECRET},
        ]))
        self.assertEqual(value.metadata()["permission"], "full-access")
        self.assertEqual(value.revision, 4)
        self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_structural_permission_change_cannot_reuse_projected_scalar(self):
        for path in (["sandboxPolicy", "networkAccess", "future_flag"],
                     ["activePermissionProfile", "id", "future_flag"],
                     ["approvalsReviewer", "future_flag"]):
            with self.subTest(path=path):
                value = projection()
                value.consume(self.event())
                with self.assertRaises(bridge.SnapshotRequired):
                    value.consume(patches([{"op": "add", "path": ["latestThreadSettings", *path], "value": SECRET}]))
                self.assertEqual(value.metadata()["permission"], "unknown")
                self.assertNotIn(SECRET, json.dumps(value.__dict__))

    def test_disconnect_resets_permission(self):
        value = projection()
        value.consume(self.event())
        value.invalidate("disconnected")
        self.assertEqual(value.metadata()["permission"], "unknown")


class FakeSocket:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def recv(self, count):
        if self.incoming:
            return self.incoming.pop(0)
        raise socket.timeout()

    def sendall(self, data):
        self.sent.extend(bridge.FrameReader().feed(data))

    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "cache.json"
        self.client = bridge.LocalChatClient(CHAT, socket_path=Path("/not-used"), output=self.output)
        self.client.projection = projection()

    def test_rpc_response_and_following_patch_in_one_recv_are_both_processed(self):
        self.client.projection.consume(snapshot())
        response = {"type": "response", "requestId": "fixed-id", "resultType": "success", "handledByClientId": OWNER,
                    "result": {"settings": {"model": "gpt-6.1-sol"}}}
        event = patches([{"op": "replace", "path": ["latestCollaborationMode", "mode"], "value": "plan"}])
        fake = FakeSocket([bridge.encode_frame(response) + bridge.encode_frame(event)])
        self.client.socket = fake
        with patch.object(bridge.uuid, "uuid4", return_value="fixed-id"):
            result = self.client._request("thread-follower-read-model-settings", {"conversationId": CHAT}, target_id=OWNER)
        self.assertEqual(result, response)
        self.assertEqual(self.client.projection.metadata()["mode"], "plan")
        self.assertEqual(json.loads(self.output.read_text())["mode"], "plan")
        self.assertEqual(fake.sent[0]["targetClientId"], OWNER)

    def test_discovery_never_claims_to_handle_desktop_requests(self):
        fake = FakeSocket()
        self.client.socket = fake
        self.client._handle_control({"type": "client-discovery-request", "requestId": "r", "request": {"content": SECRET}})
        self.assertEqual(fake.sent, [{"type": "client-discovery-response", "requestId": "r", "response": {"canHandle": False}}])
        self.assertNotIn(SECRET, json.dumps(fake.sent))

    def test_owner_disconnect_and_connection_reset_are_fatal(self):
        for event in ({"type": "broadcast", "method": "ipc-connection-reset", "params": {}},
                      {"type": "broadcast", "method": "client-status-changed", "params": {"clientId": OWNER, "status": "disconnected"}}):
            with self.assertRaises(bridge.BridgeError):
                self.client._handle_control(event)

    def test_revision_gap_requests_new_snapshot_and_clears_cache(self):
        self.client.socket = FakeSocket()
        self.client.projection.consume(snapshot())
        self.client._handle_envelope(patches([], base=9, revision=10))
        self.assertEqual(json.loads(self.output.read_text())["connection"], "waiting")
        self.assertEqual(self.client.socket.sent[0]["params"]["following"], True)
        self.assertEqual(self.client.socket.sent[0]["targetClientIds"], [OWNER])

    def test_unknown_permission_patch_clears_cache_before_resubscribe(self):
        self.client.socket = FakeSocket()
        self.client.projection.consume(snapshot())
        self.client.write()
        event = patches([{"op": "add", "path": ["latestThreadSettings", "approvalPolicy", "future_flag"], "value": SECRET}])
        self.assertFalse(self.client._handle_envelope(event))
        cache = json.loads(self.output.read_text())
        self.assertEqual((cache["connection"], cache["tier"], cache["permission"]), ("waiting", "unknown", "unknown"))
        self.assertEqual(self.client.socket.sent[0]["params"]["following"], True)
        self.assertNotIn(SECRET, self.output.read_text())
        self.assertTrue(self.client._handle_envelope(snapshot(revision=4)))
        self.assertEqual(json.loads(self.output.read_text())["tier"], "fast")

    def test_close_unsubscribes_and_clears_previous_cache(self):
        fake = FakeSocket()
        self.client.socket = fake
        self.client.projection.consume(snapshot())
        self.client.close()
        self.assertEqual(fake.sent[0]["params"]["following"], False)
        self.assertTrue(fake.closed)
        self.assertEqual(json.loads(self.output.read_text())["connection"], "disconnected")
        self.assertEqual(self.client.projection.settings, {})

    def test_readonly_liveness_rejects_wrong_owner_or_missing_settings(self):
        for response in ({"handledByClientId": "other", "result": {"settings": {}}},
                         {"handledByClientId": OWNER, "result": {"settings": None}}):
            with patch.object(self.client, "_request", return_value=response), self.assertRaises(bridge.BridgeError):
                self.client.check_liveness()

    def test_readonly_liveness_uses_settings_metadata_without_mode_inference(self):
        self.client.projection.consume(snapshot())
        with patch.object(self.client, "_request", return_value={"handledByClientId": OWNER, "result": {
            "settings": {"mode": "not-collaboration"}}}) as request:
            self.client.check_liveness()
        self.assertEqual(self.client.projection.metadata()["mode"], "default")
        self.assertEqual(request.call_args.args[0], "thread-follower-read-model-settings")
        self.assertEqual(request.call_args.kwargs["target_id"], OWNER)

    def test_atomic_cache_is_private_and_contains_only_projected_metadata(self):
        self.client.projection.consume(snapshot())
        bridge.atomic_write_metadata(self.output, self.client.projection.metadata())
        self.assertNotIn(SECRET, self.output.read_text())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(list(self.output.parent.glob("*.tmp-*")), [])

    def test_socket_validation_rejects_file_and_wrong_owner(self):
        regular = Path(self.directory.name) / "ipc.sock"
        regular.write_text("not a socket")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_socket(regular)
        with patch.object(Path, "lstat", side_effect=OSError()), self.assertRaises(bridge.BridgeError):
            bridge.validate_socket(regular)


if __name__ == "__main__":
    unittest.main()
