#!/usr/bin/env python3
"""Subscribe to an explicitly named local Codex chat's live next-turn settings.

This is a data client for the desktop's existing local coordination socket, not
an app-server owner or UI client. The socket sends full conversation snapshots;
only bounded settings are retained, and raw frames are never logged or saved.
It does not discover which chat is selected in the desktop window.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys
import time
import uuid

MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_REVISION = (1 << 53) - 1
STREAM_VERSION = 11
DEFAULT_OUTPUT = Path.home() / "Library/Caches/Tab5 Codex Micro/chat-state.json"
TIERS = frozenset(("unknown", "default", "standard", "fast", "ultrafast"))
MODES = frozenset(("unknown", "default", "plan"))


class BridgeError(Exception):
    """Only constant, content-free messages should cross the CLI boundary."""


class SnapshotRequired(BridgeError):
    """The bounded projection cannot safely apply this incremental update."""


class RevisionGap(SnapshotRequired):
    pass


def valid_revision(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_REVISION


def validate_thread_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A UUID chat ID is required") from None
    if str(parsed) != value.lower():
        raise ValueError("A UUID chat ID is required")
    return str(parsed)


def encode_frame(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise BridgeError("IPC frame exceeds the bridge limit")
    return struct.pack("<I", len(payload)) + payload


class FrameReader:
    """Decode bounded, arbitrarily fragmented length-prefixed JSON frames."""
    def __init__(self):
        self.buffer = bytearray()
        self.expected: int | None = None

    def feed(self, data: bytes) -> list[dict]:
        messages = []
        cursor = 0
        while cursor < len(data):
            need = (4 if self.expected is None else self.expected) - len(self.buffer)
            take = min(need, len(data) - cursor)
            self.buffer.extend(data[cursor:cursor + take])
            cursor += take
            if self.expected is None and len(self.buffer) == 4:
                self.expected = struct.unpack("<I", self.buffer)[0]
                self.buffer.clear()
                if not 0 < self.expected <= MAX_FRAME_BYTES:
                    raise BridgeError("IPC frame exceeds the bridge limit")
            elif self.expected is not None and len(self.buffer) == self.expected:
                try:
                    message = json.loads(self.buffer.decode("utf-8"))
                except (ValueError, UnicodeError):
                    raise BridgeError("Invalid IPC frame") from None
                self.buffer.clear()
                self.expected = None
                if not isinstance(message, dict):
                    raise BridgeError("Invalid IPC envelope")
                messages.append(message)
        return messages


def _mode(value: object) -> str:
    if not isinstance(value, dict):
        return "unknown"
    mode = value.get("mode")
    return mode if isinstance(mode, str) and mode in ("default", "plan") else "unknown"


def _tier(value: object, present: bool) -> str:
    if not present:
        return "unknown"
    if value is None or value == "default":
        # Desktop _fo treats an explicit next-turn override (including null)
        # through Hft(model=None); this selects Standard rather than falling
        # back to the configured/model default. Missing remains unknown.
        return "standard"
    if not isinstance(value, str):
        return "unknown"
    return {"priority": "fast", "fast": "fast", "ultrafast": "ultrafast", "standard": "standard"}.get(value, "unknown")


PERMISSIONS = frozenset(("unknown", "full-access", "workspace-write", "read-only", "custom"))
GRANULAR_PRESET = {"granular": {"sandbox_approval": False, "rules": False,
                              "skill_approval": False, "request_permissions": True,
                              "mcp_elicitations": True}}
PERMISSION_FIELDS = frozenset(("approvalPolicy", "approvalsReviewer", "sandboxPolicy", "activePermissionProfile"))
SANDBOX_FLAGS = frozenset(("networkAccess", "excludeSlashTmp", "excludeTmpdirEnvVar"))
PROFILE_IDS = frozenset((":danger-full-access", ":workspace", ":read-only"))


def project_approval(value: object) -> object:
    if isinstance(value, str):
        return value if value in ("never", "on-request", "untrusted") else "custom"
    if not isinstance(value, dict):
        return None
    granular = value.get("granular")
    if not isinstance(granular, dict):
        return {"custom": True}
    # Extra fields change the equality check used by the desktop preset helper.
    known = frozenset(GRANULAR_PRESET["granular"])
    if set(value) != {"granular"} or any(k not in known or type(v) is not bool for k, v in granular.items()):
        return {"custom": True}
    return {"granular": {k: v for k, v in granular.items() if k in known}}


def project_sandbox(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    if "type" in value:
        kind = value["type"]
        result["type"] = kind if kind in ("dangerFullAccess", "readOnly", "workspaceWrite", "externalSandbox") else "custom"
    for flag in SANDBOX_FLAGS:
        if flag in value:
            result[flag] = value[flag] if type(value[flag]) is bool else None
    # writableRoots are intentionally discarded: paths are not needed to name
    # the desktop permission preset and must not enter the metadata cache.
    return result


def project_profile(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return {"id": None}
    identity = value.get("id")
    if not isinstance(identity, str) or not identity:
        return {"id": None}
    return {"id": identity if identity in PROFILE_IDS else "custom"}


def _permission(settings: dict) -> str:
    # Mirror installed desktop uls/B7e/V7e. The dropdown waits for all three
    # effective fields. A custom profile takes precedence over legacy presets.
    approval, sandbox = settings.get("approvalPolicy"), settings.get("sandboxPolicy")
    reviewer = settings.get("approvalsReviewer")
    if approval is None or reviewer is None or not isinstance(sandbox, dict) or not isinstance(sandbox.get("type"), str):
        return "unknown"
    profile = settings.get("activePermissionProfile")
    if isinstance(profile, dict):
        identity = profile.get("id")
        if identity is None:
            return "unknown"
        if identity == ":danger-full-access" and approval == "never" and reviewer == "user":
            return "full-access"
        if identity == ":read-only" and approval == "on-request" and reviewer == "user":
            return "read-only"
        if identity == ":workspace" and (approval == "on-request" or (approval == GRANULAR_PRESET and reviewer == "user")):
            return "workspace-write"
        # Unknown/custom profiles and unmatched built-in profiles are never
        # relabeled Full access merely because one effective policy is 'never'.
        return "custom"
    kind = sandbox["type"]
    if kind == "dangerFullAccess" and approval == "never":
        return "full-access"
    if kind == "readOnly" and approval == "on-request" and sandbox.get("networkAccess") is False:
        return "read-only"
    standard_workspace = (kind == "workspaceWrite" and all(sandbox.get(k) is False for k in SANDBOX_FLAGS))
    if standard_workspace and (approval == "on-request" or (approval == GRANULAR_PRESET and reviewer == "user")):
        return "workspace-write"
    return "custom"


def _model(value: object) -> str | None:
    # Model identifiers are metadata. Never coerce arbitrary objects to text.
    if isinstance(value, str) and 0 < len(value) <= 128 and all(c.isalnum() or c in "-._/:" for c in value):
        return value
    return None


def project_settings(state: object) -> dict:
    """Discard all transcript and tool data before retaining a projection."""
    if not isinstance(state, dict):
        return {}
    projected = {}
    if "latestThreadSettings" in state:
        settings = state["latestThreadSettings"]
        projected["latestThreadSettings"] = project_thread_settings(settings)
    if "latestCollaborationMode" in state:
        projected["latestCollaborationMode"] = project_collaboration(state["latestCollaborationMode"])
    return projected


def project_thread_settings(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    if "serviceTier" in value:
        tier = value["serviceTier"]
        result["serviceTier"] = tier if tier is None or (isinstance(tier, str) and len(tier) <= 32) else False
    if "model" in value:
        result["model"] = _model(value["model"])
    if "collaborationMode" in value:
        result["collaborationMode"] = project_collaboration(value["collaborationMode"])
    if "approvalPolicy" in value:
        result["approvalPolicy"] = project_approval(value["approvalPolicy"])
    if "sandboxPolicy" in value:
        result["sandboxPolicy"] = project_sandbox(value["sandboxPolicy"])
    if "activePermissionProfile" in value:
        result["activePermissionProfile"] = project_profile(value["activePermissionProfile"])
    if "approvalsReviewer" in value:
        reviewer = value["approvalsReviewer"]
        result["approvalsReviewer"] = reviewer if reviewer in ("user", "auto_review", "guardian_subagent") else None
    return result


def project_collaboration(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    return {"mode": mode if isinstance(mode, str) and mode in ("default", "plan") else None}


def apply_projected_patch(state: dict, patch: dict) -> dict:
    """Apply Immer settings patches without retaining unrelated patch values."""
    if not isinstance(patch, dict) or patch.get("op") not in ("add", "replace", "remove"):
        raise BridgeError("Invalid IPC patch")
    path = patch.get("path")
    if not isinstance(path, list) or len(path) > 128 or any(type(k) not in (str, int) for k in path):
        raise BridgeError("Invalid IPC patch path")
    op = patch["op"]
    if op != "remove" and "value" not in patch:
        raise BridgeError("Invalid IPC patch value")
    if not path:
        return {} if op == "remove" else project_settings(patch["value"])
    key = path[0]
    if key not in ("latestThreadSettings", "latestCollaborationMode"):
        return state
    if len(path) == 1:
        if op == "remove":
            state.pop(key, None)
        else:
            state[key] = (project_thread_settings if key == "latestThreadSettings" else project_collaboration)(patch["value"])
        return state
    leaf = path[1]
    if key == "latestCollaborationMode":
        if path[1:] != ["mode"]:
            return state
        target = state.get(key)
        if not isinstance(target, dict):
            target = state[key] = {}
        if op == "remove":
            target.pop("mode", None)
        else:
            target["mode"] = project_collaboration({"mode": patch["value"]})["mode"]
        return state
    if leaf not in ("serviceTier", "model", "collaborationMode") and leaf not in PERMISSION_FIELDS:
        return state
    nested_permission = leaf in PERMISSION_FIELDS and len(path) > 2
    if len(path) > 2 and path[1:] != ["collaborationMode", "mode"] and not nested_permission:
        return state
    target = state.get(key)
    if not isinstance(target, dict):
        target = state[key] = {}
    if nested_permission:
        remainder = path[2:]
        allowed = ((leaf == "sandboxPolicy" and len(remainder) == 1 and remainder[0] in (*SANDBOX_FLAGS, "type"))
                   or (leaf == "activePermissionProfile" and remainder == ["id"])
                   or (leaf == "approvalPolicy" and (remainder == ["granular"] or
                       (len(remainder) == 2 and remainder[0] == "granular" and remainder[1] in GRANULAR_PRESET["granular"]))))
        if not allowed:
            # Approval equality uses the entire object. Ignoring an unknown
            # field here would leave a preset selected when a full snapshot
            # would classify it as custom. Other permission projections omit
            # unknown top-level fields (notably writableRoots) deliberately.
            if (leaf in ("approvalPolicy", "approvalsReviewer")
                    or leaf == "sandboxPolicy" and remainder[0] in (*SANDBOX_FLAGS, "type")
                    or leaf == "activePermissionProfile" and remainder[0] == "id"):
                raise SnapshotRequired("IPC permission projection requires a snapshot")
            return state
        current = target.get(leaf)
        # A custom approval sentinel intentionally lost its original shape.
        # Even a known-field patch cannot reconstruct that object (for example
        # removing its last unknown field), so ask the owner for the full state.
        if not isinstance(current, dict) or leaf == "approvalPolicy" and current == {"custom": True}:
            raise SnapshotRequired("IPC permission projection requires a snapshot")
        current = deepcopy(current)
        parent = current
        if len(remainder) == 2:
            nested = parent.get(remainder[0])
            if not isinstance(nested, dict):
                nested = parent[remainder[0]] = {}
            parent = nested
        if op == "remove":
            parent.pop(remainder[-1], None)
        else:
            # The whole value is immediately reprojected below. Only a whitelisted
            # scalar/boolean subfield can survive that projection.
            parent[remainder[-1]] = patch["value"]
        target[leaf] = project_thread_settings({leaf: current})[leaf]
    elif path[1:] == ["collaborationMode", "mode"]:
        collaboration = target.get("collaborationMode")
        if not isinstance(collaboration, dict):
            collaboration = target["collaborationMode"] = {}
        if op == "remove":
            collaboration.pop("mode", None)
        else:
            collaboration["mode"] = project_collaboration({"mode": patch["value"]})["mode"]
    elif op == "remove":
        target.pop(leaf, None)
    else:
        target[leaf] = project_thread_settings({leaf: patch["value"]})[leaf]
    return state


class MetadataProjection:
    def __init__(self, thread_id: str, host_id: str = "local"):
        self.thread_id = validate_thread_id(thread_id)
        if host_id != "local":
            raise ValueError("Only the local host is supported")
        self.host_id = host_id
        self.owner_id: str | None = None
        self.client_id: str | None = None
        self.settings: dict = {}
        self.revision: int | None = None
        self.minimum_snapshot_revision = 0
        self.observed_at = 0.0
        self.connection = "waiting"

    def invalidate(self, connection: str = "waiting") -> None:
        self.settings = {}
        self.revision = None
        self.minimum_snapshot_revision = 0
        self.observed_at = 0.0
        self.connection = connection

    def consume(self, envelope: dict, *, now: float | None = None) -> bool:
        if not isinstance(envelope, dict) or envelope.get("type") != "broadcast":
            return False
        if envelope.get("method") != "thread-stream-state-changed":
            return False
        if self.owner_id is None or self.client_id is None:
            return False
        if envelope.get("version") != STREAM_VERSION or envelope.get("sourceClientId") != self.owner_id:
            return False
        targets = envelope.get("targetClientIds")
        if targets is not None and (not isinstance(targets, list) or self.client_id not in targets):
            return False
        params = envelope.get("params")
        if not isinstance(params, dict) or params.get("conversationId") != self.thread_id or params.get("hostId") != self.host_id:
            return False
        change = params.get("change")
        if not isinstance(change, dict) or not valid_revision(change.get("revision")):
            raise BridgeError("Invalid IPC state change")
        revision = change["revision"]
        if change.get("type") == "snapshot":
            if not isinstance(change.get("conversationState"), dict):
                raise BridgeError("Invalid IPC snapshot")
            # A late older snapshot must not overwrite already accepted state.
            if revision < self.minimum_snapshot_revision or self.revision is not None and revision < self.revision:
                return False
            next_settings = project_settings(change["conversationState"])
        elif change.get("type") == "patches":
            if not valid_revision(change.get("baseRevision")) or self.revision is None or change["baseRevision"] != self.revision or revision <= self.revision:
                minimum_revision = max(self.minimum_snapshot_revision, self.revision or 0, revision)
                self.invalidate()
                self.minimum_snapshot_revision = minimum_revision
                raise RevisionGap("IPC revision gap")
            patches = change.get("patches")
            if not isinstance(patches, list) or len(patches) > 100000:
                raise BridgeError("Invalid IPC patch batch")
            next_settings = deepcopy(self.settings)
            try:
                for patch in patches:
                    next_settings = apply_projected_patch(next_settings, patch)
            except SnapshotRequired:
                self.invalidate()
                self.minimum_snapshot_revision = revision
                raise
        else:
            raise BridgeError("Unsupported IPC state change")
        self.settings = next_settings
        self.revision = revision
        self.observed_at = time.time() if now is None else now
        self.connection = "live"
        return True

    def metadata(self, *, heartbeat: float | None = None) -> dict:
        settings = self.settings.get("latestThreadSettings")
        settings = settings if isinstance(settings, dict) else {}
        direct_mode = self.settings.get("latestCollaborationMode")
        mode = _mode(direct_mode) if "latestCollaborationMode" in self.settings else _mode(settings.get("collaborationMode"))
        live = self.connection == "live" and self.revision is not None
        return {"v": 1, "source": "desktop-ipc", "scope": "explicit-target",
                "thread_id": self.thread_id, "host_id": self.host_id,
                "connection": self.connection,
                "tier": _tier(settings.get("serviceTier"), "serviceTier" in settings) if live else "unknown",
                "mode": mode if live else "unknown", "permission": _permission(settings) if live else "unknown",
                "model": _model(settings.get("model")) if live else None,
                "revision": self.revision if live else 0,
                "observed_at": (heartbeat if heartbeat is not None else self.observed_at) if live else 0.0}


def atomic_write_metadata(path: Path, metadata: dict) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_socket(path: Path) -> None:
    """Use only an existing socket and directory belonging to this user."""
    try:
        info, parent = path.lstat(), path.parent.lstat()
    except OSError:
        raise BridgeError("Desktop IPC socket is unavailable") from None
    uid = os.geteuid()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or parent.st_mode & 0o022:
        raise BridgeError("Desktop IPC socket ownership is invalid")


class LocalChatClient:
    def __init__(self, thread_id: str, *, socket_path: Path, output: Path, host_id: str = "local"):
        self.projection = MetadataProjection(thread_id, host_id)
        self.socket_path = socket_path.expanduser()
        self.output = output.expanduser()
        self.socket: socket.socket | None = None
        self.reader = FrameReader()
        self.stop = False
        self.last_write = 0.0
        self.last_error = None
        self.last_snapshot_request = 0.0
        self.last_snapshot = 0.0
        self.last_liveness = 0.0

    def _send(self, envelope: dict) -> None:
        if self.socket is None:
            raise BridgeError("Desktop IPC is disconnected")
        self.socket.sendall(encode_frame(envelope))

    def _request(self, method: str, params: dict, *, version: int = 1, timeout: float = 5.0, target_id: str | None = None) -> dict:
        request_id = str(uuid.uuid4())
        self._send({"type": "request", "requestId": request_id,
                    "sourceClientId": self.projection.client_id or "initializing-client",
                    "version": version, "method": method, "params": params,
                    "timeoutMs": int(timeout * 1000),
                    **({"targetClientId": target_id} if target_id is not None else {})})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.stop:
            response = None
            # A single recv can contain the reply and a following state patch.
            # Process the whole batch before returning the matched reply.
            for envelope in self._read():
                if envelope.get("type") == "response" and envelope.get("requestId") == request_id:
                    response = envelope
                else:
                    self._handle_envelope(envelope)
            if response is not None:
                if response.get("resultType") != "success" or not isinstance(response.get("result"), dict):
                    raise BridgeError("Desktop IPC request was unavailable")
                return response
        raise BridgeError("Desktop IPC request timed out")

    def _read(self) -> list[dict]:
        if self.socket is None:
            raise BridgeError("Desktop IPC is disconnected")
        try:
            data = self.socket.recv(65536)
        except socket.timeout:
            return []
        if not data:
            raise BridgeError("Desktop IPC disconnected")
        return self.reader.feed(data)

    def _handle_control(self, envelope: dict) -> bool:
        if envelope.get("type") == "client-discovery-request":
            request_id = envelope.get("requestId")
            if isinstance(request_id, str):
                self._send({"type": "client-discovery-response", "requestId": request_id,
                            "response": {"canHandle": False}})
            return True
        if envelope.get("type") == "request":
            request_id = envelope.get("requestId")
            if isinstance(request_id, str):
                self._send({"type": "response", "requestId": request_id,
                            "resultType": "error", "error": "no-handler-for-request"})
            return True
        if envelope.get("type") == "broadcast":
            method, params = envelope.get("method"), envelope.get("params")
            if method == "ipc-connection-reset":
                raise BridgeError("Desktop IPC reset")
            if method == "client-status-changed" and isinstance(params, dict) and params.get("clientId") == self.projection.owner_id and params.get("status") == "disconnected":
                raise BridgeError("Chat owner disconnected")
        return False

    def subscribe(self, following: bool = True) -> None:
        if self.projection.client_id is None or self.projection.owner_id is None:
            return
        self._send({"type": "broadcast", "method": "thread-stream-following-changed",
                    "sourceClientId": self.projection.client_id, "version": 1,
                    "targetClientIds": [self.projection.owner_id],
                    "params": {"conversationId": self.projection.thread_id,
                               "hostId": self.projection.host_id, "following": following}})
        if following:
            self.last_snapshot_request = time.monotonic()

    def connect(self) -> None:
        validate_socket(self.socket_path)
        self.projection.invalidate()
        self.reader = FrameReader()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(0.5)
        self.socket.connect(str(self.socket_path))
        initialized = self._request("initialize", {"clientType": "tab5-status-bridge"}, version=0)
        client_id = initialized["result"].get("clientId")
        if not isinstance(client_id, str) or not client_id:
            raise BridgeError("Desktop IPC initialization failed")
        self.projection.client_id = client_id
        owner = self._request("thread-owner-discovery", {"hostId": self.projection.host_id,
                                                         "conversationId": self.projection.thread_id})
        owner_id = owner.get("handledByClientId")
        if not isinstance(owner_id, str) or not owner_id:
            raise BridgeError("Chat owner is unavailable")
        self.projection.owner_id = owner_id
        self.last_snapshot = 0.0
        self.subscribe()
        self.write()

    def write(self) -> None:
        # Live connection heartbeat is separate from a user action receipt.
        atomic_write_metadata(self.output, self.projection.metadata(heartbeat=time.time()))
        self.last_write = time.monotonic()

    def _handle_envelope(self, envelope: dict) -> bool:
        if self._handle_control(envelope):
            return False
        try:
            accepted = self.projection.consume(envelope)
        except SnapshotRequired:
            self.write()
            self.subscribe()
            return False
        if accepted:
            if envelope["params"]["change"]["type"] == "snapshot":
                self.last_snapshot = time.monotonic()
                self.last_liveness = self.last_snapshot
            self.write()
        return accepted

    def check_liveness(self) -> None:
        # This normal metadata-only read waits for pending settings updates in
        # the owner. It neither resumes nor modifies the conversation.
        response = self._request("thread-follower-read-model-settings",
                                 {"conversationId": self.projection.thread_id},
                                 version=1, timeout=2.0,
                                 target_id=self.projection.owner_id)
        if response.get("handledByClientId") != self.projection.owner_id:
            raise BridgeError("Chat owner changed")
        result = response["result"]
        if not isinstance(result, dict) or not isinstance(result.get("settings"), dict):
            raise BridgeError("Chat owner settings are unavailable")
        self.last_liveness = time.monotonic()

    def poll(self) -> bool:
        received = False
        for envelope in self._read():
            received = self._handle_envelope(envelope) or received
        now = time.monotonic()
        if self.projection.connection != "live" and now - self.last_snapshot_request >= 5:
            raise BridgeError("Chat snapshot was unavailable")
        if self.projection.connection == "live" and now - self.last_liveness >= 3:
            self.check_liveness()
        if now - self.last_write >= 1:
            self.write()
        return received

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.subscribe(False)
            except (BridgeError, OSError):
                pass
            self.socket.close()
            self.socket = None
        self.projection.invalidate("disconnected")
        self.projection.owner_id = None
        self.projection.client_id = None
        self.reader = FrameReader()
        self.write()

    def run(self, *, once: bool = False) -> dict | None:
        try:
            while not self.stop:
                try:
                    self.connect()
                    while not self.stop:
                        accepted = self.poll()
                        if once and accepted:
                            return self.projection.metadata(heartbeat=time.time())
                except (OSError, BridgeError) as error:
                    self.close()
                    self.last_error = str(error) if isinstance(error, BridgeError) else "Desktop IPC transport unavailable"
                    if once:
                        return None
                    # Constant diagnostics only; exceptions can contain payload.
                    print("Desktop chat state unavailable; retrying", file=sys.stderr, flush=True)
                    for _ in range(20):
                        if self.stop:
                            break
                        time.sleep(0.1)
            return None
        finally:
            self.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True, type=validate_thread_id)
    parser.add_argument("--host-id", default="local", choices=("local",))
    parser.add_argument("--socket", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "ipc/ipc.sock")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--once", action="store_true", help="Print one bounded metadata snapshot, then unsubscribe and clear the cache")
    args = parser.parse_args()
    lock_path = args.output.expanduser().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("The chat-state bridge is already running", file=sys.stderr)
            return 2
        client = LocalChatClient(args.thread_id, host_id=args.host_id, socket_path=args.socket, output=args.output)
        def stop(*_args):
            client.stop = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        result = client.run(once=args.once)
        if args.once:
            if result is None:
                print(client.last_error or "Live chat settings are unavailable", file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
