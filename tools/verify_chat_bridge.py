#!/usr/bin/env python3
"""Verify an explicitly tracked chat's next-turn settings through normal IPC.

Without --mutate this only reads metadata and Tab5 STATUS. With --mutate it
temporarily changes Fast and Plan one field at a time, checks the independent
live bridge cache and real device, then restores the original fields (allowing
the desktop's null/default and priority/fast service-tier canonicalizations).
Fresh-read conflict checks preserve observed edits; the desktop API has no
atomic compare-and-swap for these fields, so simultaneous edits remain a race.
It never sends messages, starts/resumes a turn, operates UI or emits HID input.
Raw collaboration settings are kept only in RAM and never included in reports.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import tempfile
import time

from chat_state_bridge import (BridgeError, DEFAULT_OUTPUT, LocalChatClient,
                               MetadataProjection, validate_thread_id)
from verify_device import status as device_status


class QAError(Exception):
    """Constant, content-free errors safe for the QA report."""


MISSING = object()


def same_field(field: str, left, right) -> bool:
    """Recognize only the desktop's known service-tier canonicalizations."""
    if field != "serviceTier":
        return left == right
    if left is MISSING or right is MISSING:
        return left is right
    if not (left is None or type(left) is str) or not (right is None or type(right) is str):
        return False
    def canonical(value):
        if value is None or value == "default":
            return "default"
        return "priority" if value in ("priority", "fast") else value
    return canonical(left) == canonical(right)


def bounded_tier(value) -> dict:
    """Diagnostic restore hint, excluding arbitrary custom strings/objects."""
    if value is None:
        return {"type": "null", "value": None}
    if type(value) is str:
        known = ("default", "standard", "priority", "fast", "ultrafast")
        return {"type": "string", "value": value if value in known else "other"}
    return {"type": "missing" if value is MISSING else "invalid", "value": "unknown"}


def raw_projection(state: object) -> dict:
    """Retain only the two restore fields; never retain conversation history."""
    if not isinstance(state, dict):
        return {}
    result = {}
    settings = state.get("latestThreadSettings")
    if isinstance(settings, dict):
        result["latestThreadSettings"] = {
            key: deepcopy(settings[key]) for key in ("serviceTier", "collaborationMode")
            if key in settings}
    if "latestCollaborationMode" in state:
        result["latestCollaborationMode"] = deepcopy(state["latestCollaborationMode"])
    return result


def raw_patch(state: dict, patch: dict) -> dict:
    path, op = patch["path"], patch["op"]
    if not path:
        return {} if op == "remove" else raw_projection(patch["value"])
    if path[0] not in ("latestThreadSettings", "latestCollaborationMode"):
        return state
    if path[0] == "latestThreadSettings":
        if len(path) == 1:
            if op == "remove":
                state.pop(path[0], None)
            else:
                projected = raw_projection({path[0]: patch["value"]})
                state[path[0]] = projected.get(path[0], {})
            return state
        if path[1] not in ("serviceTier", "collaborationMode"):
            return state
        if path[1] == "serviceTier" and len(path) != 2:
            raise QAError("Invalid raw settings patch")
    if len(path) > 8 or any(not isinstance(key, str) for key in path):
        raise QAError("Invalid raw settings patch")
    cursor = state
    for key in path[:-1]:
        if not isinstance(cursor.get(key), dict):
            cursor[key] = {}
        cursor = cursor[key]
    if op == "remove":
        cursor.pop(path[-1], None)
    else:
        cursor[path[-1]] = deepcopy(patch["value"])
    return state


class RawProjection(MetadataProjection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raw = {}

    def invalidate(self, connection: str = "waiting") -> None:
        super().invalidate(connection)
        self.raw = {}

    def consume(self, envelope: dict, **kwargs) -> bool:
        accepted = super().consume(envelope, **kwargs)
        if not accepted:
            return False
        change = envelope["params"]["change"]
        if change["type"] == "snapshot":
            self.raw = raw_projection(change["conversationState"])
        else:
            candidate = deepcopy(self.raw)
            for patch in change["patches"]:
                candidate = raw_patch(candidate, patch)
            self.raw = candidate
        return True

    def field(self, name: str):
        settings = self.raw.get("latestThreadSettings", {})
        if name == "serviceTier":
            return settings.get(name, MISSING)
        if "latestCollaborationMode" in self.raw:
            return self.raw["latestCollaborationMode"]
        return settings.get(name, MISSING)


class QAClient(LocalChatClient):
    def __init__(self, thread_id: str, **kwargs):
        super().__init__(thread_id, **kwargs)
        self.projection = RawProjection(thread_id)

    def refresh(self, timeout: float) -> None:
        before = self.last_snapshot
        self.subscribe()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self.last_snapshot > before and self.projection.connection == "live":
                return
        raise QAError("Fresh target snapshot was unavailable")

    def update(self, field: str, value, timeout: float) -> None:
        response = self._request(
            "thread-follower-update-thread-settings",
            {"conversationId": self.projection.thread_id,
             "threadSettings": {field: deepcopy(value)}},
            version=2, timeout=timeout, target_id=self.projection.owner_id)
        if (response.get("handledByClientId") != self.projection.owner_id
                or response.get("method") != "thread-follower-update-thread-settings"
                or response["result"].get("applied") is not True):
            raise QAError("Target settings update was not accepted")


def validate_baseline(client: QAClient) -> dict:
    tier = client.projection.field("serviceTier")
    mode = client.projection.field("collaborationMode")
    if tier is MISSING or not (tier is None or isinstance(tier, str) and 0 < len(tier) <= 32):
        raise QAError("Explicit service tier is missing from the target snapshot")
    if not isinstance(mode, dict) or mode.get("mode") not in ("default", "plan"):
        raise QAError("Complete collaboration mode is missing from the target snapshot")
    settings = mode.get("settings")
    if (not isinstance(settings, dict) or not isinstance(settings.get("model"), str)
            or not settings["model"] or len(settings["model"]) > 128
            or not (settings.get("reasoning_effort") is None
                    or isinstance(settings.get("reasoning_effort"), str))
            or not (settings.get("developer_instructions") is None
                    or isinstance(settings.get("developer_instructions"), str))):
        raise QAError("Complete collaboration settings are unavailable")
    try:
        size = len(json.dumps(mode, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise QAError("Collaboration settings cannot be safely restored") from None
    if size > 65536:
        raise QAError("Collaboration settings exceed the QA limit")
    return {"serviceTier": deepcopy(tier), "collaborationMode": deepcopy(mode)}


def expected(client: QAClient, field: str, value) -> dict:
    metadata = client.projection.metadata()
    if field == "serviceTier":
        metadata["tier"] = "standard" if value is None or value == "default" else {
            "priority": "fast", "fast": "fast", "ultrafast": "ultrafast",
            "standard": "standard"}.get(value, "unknown")
    else:
        metadata["mode"] = value["mode"]
    return {"tier": metadata["tier"], "mode": metadata["mode"]}


def read_cache(path: Path, thread_id: str) -> dict | None:
    try:
        if path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if (not isinstance(value, dict) or value.get("source") != "desktop-ipc"
            or value.get("scope") != "explicit-target" or value.get("host_id") != "local"
            or value.get("thread_id") != thread_id or value.get("connection") != "live"
            or type(value.get("observed_at")) not in (int, float)
            or not 0 <= time.time() - value["observed_at"] <= 7.5):
        return None
    return value


def wait_visible(client: QAClient, target: dict, cache: Path, port: str | None,
                 timeout: float, field: str | None = None, raw=MISSING) -> dict:
    start = time.monotonic()
    deadline = start + timeout
    last_check = 0.0
    while time.monotonic() < deadline:
        client.poll()
        own = client.projection.metadata()
        local = read_cache(cache, client.projection.thread_id)
        if (own["connection"] != "live" or any(own[k] != target[k] for k in ("tier", "mode"))
                or field is not None and not same_field(field, client.projection.field(field), raw)
                or local is None or any(local.get(k) != target[k] for k in ("tier", "mode"))):
            continue
        if time.monotonic() - last_check < .3:
            continue
        last_check = time.monotonic()
        try:
            device = device_status(port, min(2.0, max(.2, deadline - time.monotonic())))["status"]
        except (OSError, RuntimeError, TimeoutError):
            continue
        chat = device.get("host_feedback", {}).get("chat", {})
        if (not device.get("lighting_preview") and chat.get("fresh") is True
                and chat.get("source") == "desktop-ipc" and chat.get("scope") == "explicit-target"
                and chat.get("thread_id") == client.projection.thread_id
                and chat.get("speed") == target["tier"] and chat.get("mode") == target["mode"]):
            return {"tier": target["tier"], "mode": target["mode"],
                    "observed_tier": bounded_tier(client.projection.field("serviceTier")),
                    "elapsed_ms": round((time.monotonic() - start) * 1000),
                    "cache_revision": local.get("revision"), "device_fresh": True}
    raise QAError("Independent live cache and device did not reach the expected settings")


def exercise(client: QAClient, field: str, values: list, baseline: dict,
             cache: Path, port: str | None, timeout: float, report: dict) -> None:
    original = baseline[field]
    last_written = MISSING
    attempted = False
    failure = None
    entry = {"field": field, "states": [], "restored": False, "conflict": False}
    if field == "serviceTier":
        entry["original_tier"] = bounded_tier(original)
    report["checks"].append(entry)
    try:
        for value in values:
            client.refresh(timeout)
            current = client.projection.field(field)
            before = original if last_written is MISSING else last_written
            if not same_field(field, current, before):
                entry["conflict"] = True
                raise QAError("Concurrent target settings change was preserved")
            target = expected(client, field, value)
            # Mark before sending: a timeout may follow an accepted mutation.
            last_written, attempted = deepcopy(value), True
            client.update(field, value, timeout)
            entry["states"].append(wait_visible(client, target, cache, port, timeout, field, value))
    except BaseException as error:
        failure = error
    finally:
        if attempted:
            try:
                client.refresh(timeout)
                current = client.projection.field(field)
                if same_field(field, current, original):
                    entry["restored"] = True
                    if field == "serviceTier":
                        entry["restored_tier"] = bounded_tier(current)
                elif not same_field(field, current, last_written):
                    entry["conflict"] = True
                    raise QAError("Concurrent target settings change prevented restoration")
                else:
                    target = expected(client, field, original)
                    client.update(field, original, timeout)
                    entry["restoration"] = wait_visible(client, target, cache, port, timeout, field, original)
                    entry["restored"] = True
            except BaseException as error:
                entry["restore_error"] = str(error) if isinstance(error, QAError) else type(error).__name__
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure


def verify(args) -> dict:
    report = {"v": 1, "success": False, "scope": "explicit-target",
              "thread_id": args.thread_id, "mutate": args.mutate, "checks": [],
              "concurrency": "fresh-read conflict checks; no atomic tier/mode compare-and-swap",
              "evidence": "desktop next-turn settings, independent cache and actual device STATUS"}
    # This private output never takes/clobbers the production bridge's lock/cache.
    with tempfile.TemporaryDirectory(prefix="tab5-chat-qa-") as directory:
        client = QAClient(args.thread_id, socket_path=args.socket,
                          output=Path(directory) / "qa-metadata.json")
        def interrupted(*_):
            raise KeyboardInterrupt
        previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        try:
            client.connect()
            client.refresh(args.timeout)
            baseline = validate_baseline(client)
            report["original_tier"] = bounded_tier(baseline["serviceTier"])
            initial = client.projection.metadata()
            target = {key: initial[key] for key in ("tier", "mode")}
            report["initial"] = wait_visible(client, target, args.live_cache, args.port, args.timeout)
            if args.mutate:
                exercise(client, "serviceTier", [None, args.fast_tier], baseline,
                         args.live_cache, args.port, args.timeout, report)
                alternate = deepcopy(baseline["collaborationMode"])
                alternate["mode"] = "default" if alternate["mode"] == "plan" else "plan"
                exercise(client, "collaborationMode", [alternate], baseline,
                         args.live_cache, args.port, args.timeout, report)
                client.refresh(args.timeout)
                if any(not same_field(key, client.projection.field(key), baseline[key]) for key in baseline):
                    raise QAError("Final settings differ from the original baseline")
            report["success"] = True
        except BaseException as error:
            report["error"] = str(error) if isinstance(error, QAError) else type(error).__name__
        finally:
            try:
                client.close()
            except (BridgeError, OSError):
                report["cleanup_error"] = "IPC cleanup was unavailable"
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True, type=validate_thread_id)
    parser.add_argument("--mutate", action="store_true", help="Temporarily test Fast/Plan and restore original settings")
    parser.add_argument("--socket", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "ipc/ipc.sock")
    parser.add_argument("--live-cache", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--port", default=None)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--fast-tier", choices=("priority", "fast"), default="priority")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 3 <= args.timeout <= 30:
        parser.error("--timeout must be between 3 and 30 seconds")
    report = verify(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.output.expanduser().write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
