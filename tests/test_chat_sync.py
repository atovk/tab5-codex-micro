"""Only live, explicit desktop metadata may change the displayed chat modes."""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chat_sync
import desktop_sync


CHAT = "00000000-0000-0000-0000-000000000001"


class ChatSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "chat.json"
        self.db = self.root / "state.sqlite"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE threads (id TEXT, name TEXT, archived INTEGER)")
            db.execute("INSERT INTO threads VALUES (?, ?, 0)", (CHAT, "演示聊天标题"))
            # No turn/message table exists; the reader must not require one.
        self.data = dict(v=1, source="desktop-ipc", scope="explicit-target", host_id="local",
                         thread_id=CHAT, connection="live", tier="fast", mode="plan",
                         observed_at=1000., revision=5)

    def read(self, **changes):
        self.cache.write_text(json.dumps({**self.data, **changes}), encoding="utf-8")
        return chat_sync.build_snapshot(self.cache, self.db, now=1001.)

    def test_exact_live_target_and_real_modes(self):
        result = self.read()
        self.assertEqual((result.thread_id, result.speed, result.mode), (CHAT, "fast", "plan"))
        self.assertEqual(result.title, "演示聊天标题")
        self.assertEqual(result.scope, "explicit-target")

    def test_expired_and_future_clock_clear_modes_and_identity(self):
        for stamp in (994., 1003., float("nan"), float("inf"), True, "1000"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.read(observed_at=stamp), chat_sync.ChatSnapshot())

    def test_disconnected_waiting_or_wrong_scope_clear(self):
        for field, values in {"connection": ["waiting", "disconnected", None],
                              "scope": ["current-chat", "recent", None],
                              "host_id": ["remote", None], "source": ["local-config", None]}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.read(**{field: value}), chat_sync.ChatSnapshot())

    def test_invalid_version_identity_revision_or_enums_clear(self):
        for field, values in {"v": [True, 2], "thread_id": ["recent", "", None],
                              "revision": [True, -1, None], "tier": ["priority", [], None],
                              "mode": ["planning", {}, None]}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.read(**{field: value}), chat_sync.ChatSnapshot())

    def test_default_remains_distinct_from_standard(self):
        self.assertEqual(self.read(tier="default").speed, "default")
        self.assertEqual(self.read(tier="standard", mode="default").mode, "default")

    def test_permission_is_independent_and_never_defaults_to_full_access(self):
        self.assertEqual(self.read().permission, "unknown")
        self.assertEqual(self.read(permission="full-access").permission, "full-access")
        self.assertEqual(self.read(permission="custom").permission, "custom")
        self.assertEqual(self.read(permission="never"), chat_sync.ChatSnapshot())

    def test_no_cache_or_malformed_cache_clears(self):
        self.assertEqual(chat_sync.build_snapshot(self.cache, self.db), chat_sync.ChatSnapshot())
        for text in ("[]", "{", "x" * 8193):
            self.cache.write_text(text)
            self.assertEqual(chat_sync.build_snapshot(self.cache, self.db), chat_sync.ChatSnapshot())

    def test_title_not_required_for_real_modes(self):
        self.db.unlink()
        result = self.read()
        self.assertEqual((result.speed, result.mode, result.title), ("fast", "plan", ""))

    def test_title_sanitized_utf8_and_bounded(self):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE threads SET name=?", ("界\n面\x00 " + "测" * 100,))
        title = self.read().title
        self.assertLessEqual(len(title.encode("utf-8")), 191)
        self.assertFalse(any(ord(c) < 32 for c in title))
        self.assertTrue(title.startswith("界 面"))

    def test_summary_omits_identity_title_and_extra_content(self):
        result = self.read(transcript="PRIVATE-PROMPT-DO-NOT-SEND")
        shown = json.dumps(chat_sync.summary(result))
        self.assertNotIn(CHAT, shown)
        self.assertNotIn("部署", shown)
        self.assertNotIn("PRIVATE", shown)
        self.assertNotIn("PRIVATE", json.dumps(chat_sync.payload(result)))

    def test_wire_joins_metadata_without_relabeling_saved_defaults(self):
        result = desktop_sync.DesktopSnapshot("local-config", "unknown", "composer-navigation",
                                              "push-to-talk", ("fast",) * 7, chat=self.read())
        payload = json.loads(desktop_sync.encode_command(result, 9)[len(b"HOST_CONFIG "):])
        self.assertEqual(payload["tier"], "unknown")
        self.assertEqual(payload["chat"]["speed"], "fast")
        self.assertEqual(payload["chat"]["mode"], "plan")
        self.assertEqual(payload["chat"]["thread_id"], CHAT)
        self.assertEqual(desktop_sync.summary(result)["scope"], "configuration-defaults")

    def test_unavailable_wire_clears_and_rejects_forged_modes(self):
        self.assertEqual(chat_sync.payload(chat_sync.ChatSnapshot())["speed"], "unknown")
        with self.assertRaises(ValueError):
            chat_sync.payload(chat_sync.ChatSnapshot(speed="fast"))


if __name__ == "__main__":
    unittest.main()
