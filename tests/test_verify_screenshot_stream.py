from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import verify_screenshot_stream as qa


class BytesConnection:
    def __init__(self, data: bytes, chunk: int = 4096):
        self.data = bytearray(data)
        self.chunk = chunk
        self.writes = []

    def read(self, count):
        take = min(count, self.chunk, len(self.data))
        result = bytes(self.data[:take])
        del self.data[:take]
        return result

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass


def pixels():
    # These valid pixel bytes deliberately resemble diagnostic protocol text.
    pattern = b'\nINPUT_EVENT {"m":"v.oai.hid"}\nEND_FRAME\nSTATUS\x00\xff'
    return (pattern * (qa.PIXEL_BYTES // len(pattern) + 1))[:qa.PIXEL_BYTES]


class FrameTests(unittest.TestCase):
    def test_body_is_exact_binary_not_a_text_scan_and_does_not_consume_following_text(self):
        body = pixels()
        connection = BytesConnection(body + qa.TRAILER + b"NEXT\n", chunk=713)
        reader = qa.FrameReader()
        result = reader.read(connection, time.monotonic() + 10)
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["bytes"], qa.PIXEL_BYTES)
        self.assertTrue(reader.complete)
        self.assertEqual(bytes(connection.data), b"NEXT\n")

    def test_injected_text_shifts_exact_frame_boundary_and_is_rejected(self):
        body = pixels()
        corrupted = body[:100] + b"INPUT_EVENT {}\n" + body[100:]
        with self.assertRaisesRegex(RuntimeError, "trailer"):
            qa.FrameReader().read(BytesConnection(corrupted + qa.TRAILER), time.monotonic() + 10)

    def test_same_length_corruption_changes_hash_even_with_valid_trailer(self):
        body = pixels()
        corrupt = bytes([body[0] ^ 1]) + body[1:]
        result = qa.FrameReader().read(BytesConnection(corrupt + qa.TRAILER), time.monotonic() + 10)
        self.assertNotEqual(result["sha256"], hashlib.sha256(body).hexdigest())

    def test_trailer_is_exact_including_newlines(self):
        for trailer in (b"\nEND_FRAMX\n", b"\r\nEND_FRAME", b"END_FRAME\n!"):
            with self.subTest(trailer=trailer), self.assertRaisesRegex(RuntimeError, "trailer"):
                qa.FrameReader().read(BytesConnection(pixels() + trailer), time.monotonic() + 10)

    def test_read_failure_resumes_remaining_bytes_without_resetting_digest(self):
        body = pixels()
        connection = BytesConnection(body + qa.TRAILER)
        read = connection.read
        calls = 0

        def interrupted(count):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise TimeoutError("Synthetic interrupted transfer")
            return read(count)

        reader = qa.FrameReader()
        with patch.object(connection, "read", side_effect=interrupted), self.assertRaises(TimeoutError):
            reader.read(connection, time.monotonic() + 10)
        self.assertEqual(reader.received, 3 * 4096)
        result = reader.read(connection, time.monotonic() + 10)
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())

    def test_host_reads_continuously_without_sleep_and_reports_complete_progress(self):
        progress = []
        with patch.object(qa, "quiet") as sleep:
            result = qa.FrameReader().read(BytesConnection(pixels() + qa.TRAILER),
                time.monotonic() + 10, on_progress=progress.append)
        sleep.assert_not_called()
        self.assertEqual(result["bytes"], qa.PIXEL_BYTES)
        self.assertEqual(progress[0]["remaining_bytes"], qa.PIXEL_BYTES)
        self.assertEqual(progress[-1]["remaining_bytes"], 0)
        self.assertTrue(progress[-1]["complete"])


class SessionTests(unittest.TestCase):
    def test_pipelined_status_and_release_are_parsed_only_after_exact_trailer(self):
        body = pixels()
        status = {"firmware": "tab5-micro-0.11.1", "input_capture": False}
        wire = (b"FRAME 1280 720 RGB565_BE\n" + body + qa.TRAILER
                + b"INPUT_EVENT " + json.dumps(qa.RELEASE).encode() + b"\n"
                + json.dumps(status).encode() + b"\n")
        connection = BytesConnection(wire)
        session = qa.StreamSession(connection, 90)
        session.events = [qa.PRESS]
        session.frame(slow=True, pipeline_status=True)
        self.assertEqual(connection.writes, [b"SCREEN_TEST_SLOW\nSTATUS\n"])
        self.assertEqual(session.events, [qa.PRESS])
        self.assertEqual(session.queued_status(time.monotonic() + 10), status)
        self.assertEqual(session.events, [qa.PRESS, qa.RELEASE])

    def test_slow_command_explicitly_rejected_without_capture(self):
        connection = BytesConnection(b"SCREEN_TEST_ERROR\n")
        session = qa.StreamSession(connection, 90)
        session.reject_slow_without_capture()
        self.assertEqual(connection.writes, [b"SCREEN_TEST_SLOW\n"])
        self.assertFalse(session.request_pending)

    def test_unexpected_frame_on_gate_check_retains_binary_recovery_state(self):
        connection = BytesConnection(b"FRAME 1280 720 RGB565_BE\n" + pixels() + qa.TRAILER)
        session = qa.StreamSession(connection, 90)
        with self.assertRaisesRegex(RuntimeError, "not explicitly rejected"):
            session.reject_slow_without_capture()
        self.assertTrue(session.request_pending)
        self.assertEqual(session.reader.remaining, qa.PIXEL_BYTES)
        self.assertEqual(len(connection.data), qa.PIXEL_BYTES + len(qa.TRAILER))

    def test_status_parser_is_gated_when_a_frame_is_pending(self):
        session = qa.StreamSession(BytesConnection(b"{}\n"), 90)
        session.request_pending = True
        with self.assertRaisesRegex(RuntimeError, "inside a binary frame"):
            session.queued_status(time.monotonic() + 10)

    def test_unknown_dimensions_are_rejected_without_allocating_or_scanning_body(self):
        connection = BytesConnection(b"FRAME 1280 721 RGB565_BE\n" + b"pixels")
        session = qa.StreamSession(connection, 90)
        with self.assertRaisesRegex(RuntimeError, "header"):
            session.frame()
        self.assertTrue(session.request_pending)
        self.assertIsNone(session.reader)
        self.assertEqual(bytes(connection.data), b"pixels")

    def test_quarantine_waits_for_both_watchdogs_before_status_probe(self):
        clock = [0.0]
        connection = BytesConnection(b"")

        def idle(_count):
            clock[0] += .25
            return b""

        with patch.object(qa.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(connection, "read", side_effect=idle):
            session = qa.StreamSession(connection, 90)
            session.request_pending = True
            with patch.object(session, "status", return_value={"diagnostic_frame_active": False,
                                                               "input_capture": False}) as status:
                result = session.recover_stream(0)
        self.assertGreaterEqual(clock[0], 65)
        self.assertTrue(result["watchdog_quarantine_confirmed"])
        status.assert_called_once()
        self.assertEqual(connection.writes, [])

    def test_report_metadata_excludes_chat_text(self):
        value = qa.safe_status({"firmware": "tab5-micro-0.11.1", "host_feedback": {
            "chat": {"title": "private title", "thread_id": "private id"}},
            "diagnostic_lines_dropped": 0})
        self.assertNotIn("private", json.dumps(value))
        self.assertEqual(value["diagnostic_lines_dropped"], 0)


if __name__ == "__main__":
    unittest.main()
