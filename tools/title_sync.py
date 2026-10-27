#!/usr/bin/env python3
"""Sync explicitly assigned chat titles and local configuration defaults to Tab5.

The default invocation is a dry run with counts and configuration enums only.
--show-titles includes the six title rows. --once sends both packets; --watch
refreshes metadata every second and sends a heartbeat every two seconds.
Only the exact fixed custom assignments are supported. Dynamic ordering is
never inferred from chat recency, and unsupported input clears all titles.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import tomllib
import unicodedata
from urllib.parse import quote

import desktop_sync
import chat_sync
from tab5_transport import SerialBusyError, open_serial


SLOT_COUNT = 6
MAX_COMMAND_BYTES = 6144
MAX_KEY_BYTES = 95
MAX_TITLE_BYTES = 191
HEARTBEAT_SECONDS = 2.0
POLL_SECONDS = 1.0
ACK_TIMEOUT_SECONDS = 1.5
SOURCE_KEY = "codex-micro-agent-source"
ASSIGNMENTS_KEY = "codex-micro-custom-agent-assignments"
LOCAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")


@dataclass(frozen=True)
class Slot:
    id: int
    key: str = ""
    title: str = ""


@dataclass(frozen=True)
class Snapshot:
    source: str
    slots: tuple[Slot, ...]
    reason: str | None = None
    configured: int = 0
    resolved: int = 0


def unavailable(reason: str, configured: int = 0) -> Snapshot:
    return Snapshot("unavailable", tuple(Slot(i) for i in range(SLOT_COUNT)), reason, configured)


def normalize_title(text: str | None, max_bytes: int = MAX_TITLE_BYTES) -> str:
    if max_bytes < 0:
        raise ValueError("Title byte limit must be nonnegative")
    if text is None:
        return ""
    if not isinstance(text, str):
        raise ValueError("Title must be a string")
    cleaned = "".join(" " if character.isspace() else character
                      for character in text
                      if character.isspace() or unicodedata.category(character) != "Cc")
    cleaned = " ".join(cleaned.split())
    encoded = cleaned.encode("utf-8")
    return cleaned if len(encoded) <= max_bytes else encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def read_local_titles(db_path: Path, thread_ids: list[str]) -> dict[str, str]:
    """Read only requested IDs and names; no messages, turns or rollouts."""
    if not thread_ids:
        return {}
    uri = "file:" + quote(str(db_path.expanduser().resolve())) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.2)
    try:
        connection.execute("PRAGMA query_only = ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        if not {"id", "name", "archived"}.issubset(columns):
            raise ValueError("Local title database has an unsupported schema")
        placeholders = ",".join("?" for _ in thread_ids)
        rows = connection.execute(
            f"SELECT id, name FROM threads WHERE archived = 0 AND id IN ({placeholders})",
            thread_ids,
        ).fetchall()
        return {identifier: normalize_title(name) or "Untitled chat" for identifier, name in rows}
    finally:
        connection.close()


def build_snapshot(state_path: Path, db_path: Path, config_path: Path | None = None) -> Snapshot:
    """Resolve custom rows by their explicit host and thread identity."""
    try:
        state = json.loads(state_path.expanduser().read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return unavailable("Global state must be an object")
        source = state.get(SOURCE_KEY, "recent")
        # New desktop builds migrate registered settings into [desktop]. The
        # unregistered custom assignment atom remains in global state.
        config_path = config_path or state_path.parent / "config.toml"
        if config_path.exists():
            with config_path.open("rb") as stream:
                desktop = tomllib.load(stream).get("desktop", {})
            if not isinstance(desktop, dict):
                return unavailable("Desktop settings must be an object")
            source = desktop.get(SOURCE_KEY, source)
        if source != "custom":
            return unavailable("Codex Micro source is not custom; dynamic slot order is unsupported")
        atoms = state.get("electron-persisted-atom-state", {})
        if not isinstance(atoms, dict):
            return unavailable("Persisted metadata must be an object")
        assignments = atoms.get(ASSIGNMENTS_KEY, {})
        if not isinstance(assignments, dict):
            return unavailable("Custom assignments must be an object")
        unknown_keys = set(assignments) - {f"AG{i:02d}" for i in range(SLOT_COUNT)}
        if unknown_keys:
            return unavailable("Custom assignments contain unsupported slots")
        entries: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for index in range(SLOT_COUNT):
            assignment = assignments.get(f"AG{index:02d}")
            if assignment is None:
                continue
            if not isinstance(assignment, dict) or "type" in assignment:
                return unavailable("A custom slot is an action rather than a supported local chat", len(entries))
            key = assignment.get("threadKey")
            if assignment.get("hostId") != "local" or not isinstance(key, str) or not key.startswith("local:"):
                return unavailable("A custom slot needs an explicit local host and thread identity", len(entries))
            if len(key.encode("utf-8")) > MAX_KEY_BYTES:
                return unavailable("A custom slot identity exceeds the device byte limit", len(entries))
            identifier = key.removeprefix("local:")
            if not LOCAL_ID.fullmatch(identifier) or identifier.startswith("client-new-thread:"):
                return unavailable("A custom slot has an unsupported or pending thread identity", len(entries))
            if key in seen:
                return unavailable("Custom assignments contain duplicate chat identities", len(entries))
            seen.add(key)
            entries.append((index, key, identifier))
        titles = read_local_titles(db_path, [entry[2] for entry in entries])
        if any(identifier not in titles for _, _, identifier in entries):
            return unavailable("An assigned chat is missing or archived in local metadata", len(entries))
        by_slot = {index: Slot(index, key, titles[identifier]) for index, key, identifier in entries}
        return Snapshot("custom", tuple(by_slot.get(index, Slot(index)) for index in range(SLOT_COUNT)), configured=len(entries), resolved=len(entries))
    except (OSError, ValueError, sqlite3.Error, UnicodeError):
        return unavailable("Assigned chat metadata is unavailable or invalid")


def encode_command(snapshot: Snapshot, seq: int) -> bytes:
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq <= 0xFFFFFFFF:
        raise ValueError("Sequence must be a uint32")
    if snapshot.source not in ("custom", "unavailable"):
        raise ValueError("Unsupported title source")
    if len(snapshot.slots) != SLOT_COUNT or tuple(slot.id for slot in snapshot.slots) != tuple(range(SLOT_COUNT)):
        raise ValueError("Exactly six ordered title slots are required")
    if snapshot.source == "unavailable" and any(slot.key or slot.title for slot in snapshot.slots):
        raise ValueError("Unavailable title batches must clear every slot")
    slots = []
    for slot in snapshot.slots:
        if not isinstance(slot.key, str) or len(slot.key.encode("utf-8")) > MAX_KEY_BYTES:
            raise ValueError("Invalid title slot key")
        title = normalize_title(slot.title)
        if bool(slot.key) != bool(title):
            raise ValueError("A title slot must contain both identity and title, or neither")
        slots.append({"id": slot.id, "key": slot.key, "title": title})
    payload = {"v": 1, "seq": seq, "source": snapshot.source, "slots": slots}
    def pack():
        return b"TITLES " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    command = pack()
    # Quotes and backslashes can double JSON size despite the UTF-8 title cap.
    # Reduce only text, preserving the exact slot identities and valid Unicode.
    while len(command) > MAX_COMMAND_BYTES:
        longest = max(slots, key=lambda entry: len(json.dumps(entry["title"], ensure_ascii=False).encode("utf-8")))
        title = longest["title"]
        current_size = len(json.dumps(title, ensure_ascii=False).encode("utf-8"))
        target_size = max(3, current_size - (len(command) - MAX_COMMAND_BYTES))
        low, high = 1, len(title)
        if high <= 1:
            raise ValueError("Title identities exceed the command byte budget")
        while low < high:
            middle = (low + high + 1) // 2
            size = len(json.dumps(title[:middle], ensure_ascii=False).encode("utf-8"))
            if size <= target_size:
                low = middle
            else:
                high = middle - 1
        longest["title"] = title[:low]
        command = pack()
    return command


def summary(snapshot: Snapshot, show_titles: bool = False) -> dict:
    result = {"source": snapshot.source, "configured": snapshot.configured,
              "resolved": snapshot.resolved, "empty": SLOT_COUNT - snapshot.resolved,
              "command_bytes": len(encode_command(snapshot, 1))}
    if snapshot.reason:
        result["reason"] = snapshot.reason
    if show_titles:
        result["slots"] = [{"id": slot.id, "key": slot.key, "title": slot.title} for slot in snapshot.slots]
    return result


class _AckLines:
    """Keep partial serial lines across read timeouts and packet deadlines."""
    def __init__(self):
        self.pending = bytearray()
        self.discarding = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.pending.extend(chunk)
        lines = []
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending = remaining
            if self.discarding:
                self.discarding = False
            elif len(line) <= 8192:
                lines.append(bytes(line).strip())
        if len(self.pending) > 8192:
            self.pending.clear()
            self.discarding = True
        return lines


def _send_command(connection, command: bytes, seq: int, name: str, timeout: float,
                  responses: _AckLines | None = None) -> None:
    responses = responses if responses is not None else _AckLines()
    connection.write(command)
    connection.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in responses.feed(connection.read_until(b"\n", 8192)):
            if line == f"{name}_OK {seq}".encode("ascii"):
                return
            if line.startswith(f"{name}_ERROR".encode("ascii")):
                error = RuntimeError(f"Tab5 rejected the {name} batch")
                error.packet, error.failure = name, "rejected"
                raise error
    error = TimeoutError(f"Tab5 {name} acknowledgement timed out")
    error.packet, error.failure = name, "ack-timeout"
    raise error


def send_snapshot(snapshot: Snapshot, seq: int, port: str | None = None, timeout: float = 8) -> None:
    """Preserve the title-only helper for existing callers."""
    command = encode_command(snapshot, seq)
    with open_serial(port, timeout) as connection:
        _send_command(connection, command, seq, "TITLES", timeout)


def send_snapshots(snapshot: Snapshot, desktop: desktop_sync.DesktopSnapshot, seq: int,
                   port: str | None = None, timeout: float = 0.5, *,
                   ack_timeout: float = ACK_TIMEOUT_SECONDS) -> None:
    """Send both snapshots while holding the same exact-device serial lock.

    The short serial-lock wait and each packet's acknowledgement budget are
    independent. Both packets are attempted even if one ACK fails, preventing
    unavailable title acknowledgements from starving valid configuration.
    Overall success still requires both ACKs; failed pairs are retried.
    """
    if (not isinstance(ack_timeout, (int, float)) or isinstance(ack_timeout, bool)
            or not math.isfinite(ack_timeout) or ack_timeout <= 0):
        raise ValueError("Acknowledgement timeout must be finite and positive")
    commands = (("TITLES", encode_command(snapshot, seq)),
                ("HOST_CONFIG", desktop_sync.encode_command(desktop, seq)))
    with open_serial(port, timeout) as connection:
        responses = _AckLines()
        failures = []
        for name, command in commands:
            try:
                _send_command(connection, command, seq, name, ack_timeout, responses)
            except (RuntimeError, TimeoutError) as error:
                if getattr(error, "packet", None) != name:
                    # A broken write/read transport is not an independent ACK
                    # failure; another command could append to a partial write.
                    raise
                failures.append(error)
        if failures:
            # Report bounded metadata, never serial payloads or custom text.
            failures[0].failed_packets = tuple((error.packet, error.failure) for error in failures)
            raise failures[0]


def _failure_details(error: Exception) -> dict:
    packet = getattr(error, "packet", "connection")
    failure = getattr(error, "failure", "unavailable")
    result = {"packet": packet if packet in ("TITLES", "HOST_CONFIG") else "connection",
              "failure": failure if failure in ("rejected", "ack-timeout") else "unavailable"}
    failed = getattr(error, "failed_packets", ())
    if failed:
        result["failed_packets"] = [
            {"packet": name, "failure": reason} for name, reason in failed
            if name in ("TITLES", "HOST_CONFIG") and reason in ("rejected", "ack-timeout")]
    return result


def combined_summary(snapshot: Snapshot, desktop: desktop_sync.DesktopSnapshot,
                     show_titles: bool = False) -> dict:
    # Keep the original title summary keys for existing dry-run consumers.
    return {**summary(snapshot, show_titles), "desktop": desktop_sync.summary(desktop)}


def watch(state_path: Path, db_path: Path, port: str | None, show_titles: bool = False,
          config_path: Path | None = None, chat_cache: Path | None = None) -> None:
    seq = int(time.time()) & 0xFFFFFFFF
    last_sent = None
    last_notice = None
    next_heartbeat = 0.0
    failed_attempts = 0
    while True:
        snapshot = build_snapshot(state_path, db_path, config_path)
        desktop = desktop_sync.build_snapshot(state_path, config_path)
        if chat_cache is not None:
            desktop = replace(desktop, chat=chat_sync.build_snapshot(chat_cache, db_path))
        identity = (snapshot.source, snapshot.slots, desktop)
        now = time.monotonic()
        notice = (snapshot.source, snapshot.reason, snapshot.configured, snapshot.resolved, desktop)
        if notice != last_notice:
            print(json.dumps(combined_summary(snapshot, desktop, show_titles), ensure_ascii=False), flush=True)
            last_notice = notice
        if identity != last_sent or now >= next_heartbeat:
            try:
                seq = (seq + 1) & 0xFFFFFFFF
                send_snapshots(snapshot, desktop, seq, port, timeout=0.5)
                last_sent = identity
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                if getattr(watch, "_transport_notice", None) is not None:
                    print(json.dumps({"delivery": "acknowledged", "seq": seq,
                                      "packets": ["TITLES", "HOST_CONFIG"],
                                      "failed_attempts": failed_attempts}), flush=True)
                failed_attempts = 0
                transport_notice = None
                details = {}
            except SerialBusyError:
                transport_notice = "Tab5 serial interface is busy; heartbeat skipped"
                details = {"packet": "connection", "failure": "busy"}
                failed_attempts += 1
            except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                transport_notice = "Tab5 companion connection is unavailable; retrying"
                details = _failure_details(error)
                failed_attempts += 1
            notice_key = (transport_notice, details) if transport_notice else None
            if notice_key != getattr(watch, "_transport_notice", None):
                if transport_notice:
                    print(json.dumps({"transport": transport_notice, **details}), flush=True)
                watch._transport_notice = notice_key
        time.sleep(POLL_SECONDS)


def main() -> int:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--once", action="store_true", help="Send one title and configuration batch")
    mode.add_argument("--dry-run", action="store_true", help="Show source status without opening hardware (default)")
    parser.add_argument("--show-titles", action="store_true")
    parser.add_argument("--state", type=Path, default=codex_home / ".codex-global-state.json")
    parser.add_argument("--db", type=Path, default=codex_home / "state_5.sqlite")
    parser.add_argument("--config", type=Path, help="Desktop config.toml; defaults beside the state file")
    parser.add_argument("--port", help="Still requires the exact Tab5 VID/PID and serial identity")
    parser.add_argument("--chat-cache", type=Path, default=chat_sync.DEFAULT_CACHE,
                        help="Live metadata from the explicit desktop IPC subscription")
    args = parser.parse_args()
    try:
        if args.watch:
            watch(args.state, args.db, args.port, args.show_titles, args.config, args.chat_cache)
        else:
            snapshot = build_snapshot(args.state, args.db, args.config)
            desktop = desktop_sync.build_snapshot(args.state, args.config)
            desktop = replace(desktop, chat=chat_sync.build_snapshot(args.chat_cache, args.db))
            if args.once:
                send_snapshots(snapshot, desktop, int(time.time()) & 0xFFFFFFFF, args.port)
            print(json.dumps(combined_summary(snapshot, desktop, args.show_titles), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
