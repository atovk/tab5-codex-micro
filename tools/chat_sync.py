"""Validate the live desktop metadata cache for the Tab5 serial companion.

This reader never selects a chat by recency and never reads message contents.
An explicit subscription target is displayed as such, not as desktop focus.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import quote


DEFAULT_CACHE = Path.home() / "Library" / "Caches" / "Tab5 Codex Micro" / "chat-state.json"
MAX_AGE_SECONDS = 6.0
THREAD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)
SPEEDS = frozenset(("unknown", "default", "standard", "fast", "ultrafast"))
MODES = frozenset(("unknown", "default", "plan"))
PERMISSIONS = frozenset(("unknown", "full-access", "workspace-write", "read-only", "custom"))


@dataclass(frozen=True)
class ChatSnapshot:
    source: str = "unavailable"
    scope: str = "explicit-target"
    thread_id: str = ""
    title: str = ""
    speed: str = "unknown"
    mode: str = "unknown"
    permission: str = "unknown"


def _title(db_path: Path, thread_id: str) -> str:
    uri = "file:" + quote(str(db_path.expanduser().resolve())) + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=0.2) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT name FROM threads WHERE id=? AND archived=0", (thread_id,)).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    if not row or not isinstance(row[0], str):
        return ""
    cleaned = " ".join("".join(" " if c.isspace() else c for c in row[0]
                               if c.isspace() or not unicodedata.category(c).startswith("C")).split())
    return cleaned.encode("utf-8")[:191].decode("utf-8", errors="ignore").rstrip()


def build_snapshot(cache_path: Path, db_path: Path, *, now: float | None = None) -> ChatSnapshot:
    try:
        # Metadata packets are deliberately small. Do not load an arbitrary log.
        if cache_path.stat().st_size > 8192:
            return ChatSnapshot()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != 1:
            return ChatSnapshot()
        if (data.get("source") != "desktop-ipc" or data.get("scope") != "explicit-target"
                or data.get("host_id") != "local" or data.get("connection") != "live"):
            return ChatSnapshot()
        thread_id = data.get("thread_id")
        stamp = data.get("observed_at")
        revision = data.get("revision")
        if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
            return ChatSnapshot()
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            return ChatSnapshot()
        age = (time.time() if now is None else now) - stamp
        if not -1 <= age <= MAX_AGE_SECONDS or type(revision) is not int or revision < 0:
            return ChatSnapshot()
        speed, mode = data.get("tier"), data.get("mode")
        permission = data.get("permission", "unknown")
        if not isinstance(speed, str) or speed not in SPEEDS or not isinstance(mode, str) or mode not in MODES:
            return ChatSnapshot()
        if not isinstance(permission, str) or permission not in PERMISSIONS:
            return ChatSnapshot()
        return ChatSnapshot("desktop-ipc", "explicit-target", thread_id, _title(db_path, thread_id), speed, mode, permission)
    except (OSError, ValueError, UnicodeError):
        return ChatSnapshot()


def payload(snapshot: ChatSnapshot) -> dict:
    if snapshot.scope != "explicit-target" or snapshot.source not in ("desktop-ipc", "unavailable"):
        raise ValueError("Invalid live chat source")
    if snapshot.speed not in SPEEDS or snapshot.mode not in MODES or snapshot.permission not in PERMISSIONS:
        raise ValueError("Invalid live chat modes")
    if snapshot.source == "desktop-ipc":
        if not THREAD_ID.fullmatch(snapshot.thread_id) or len(snapshot.title.encode("utf-8")) > 191:
            raise ValueError("Invalid live chat identity")
        if any(ord(c) < 32 for c in snapshot.title):
            raise ValueError("Invalid live chat title")
    elif (snapshot.thread_id, snapshot.title, snapshot.speed, snapshot.mode, snapshot.permission) != ("", "", "unknown", "unknown", "unknown"):
        raise ValueError("Unavailable live chat must clear all fields")
    return {"source": snapshot.source, "scope": snapshot.scope, "thread_id": snapshot.thread_id,
            "title": snapshot.title, "speed": snapshot.speed, "mode": snapshot.mode,
            "permission": snapshot.permission}


def summary(snapshot: ChatSnapshot) -> dict:
    # Identity/title stay off ordinary service logs, as for the title companion.
    return {"source": snapshot.source, "scope": snapshot.scope, "speed": snapshot.speed,
            "mode": snapshot.mode, "permission": snapshot.permission, "has_target": bool(snapshot.thread_id)}
