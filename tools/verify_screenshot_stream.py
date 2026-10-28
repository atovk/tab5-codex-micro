#!/usr/bin/env python3
"""Verify SCREEN framing across the capture watchdog with device-side pacing.

A frozen synthetic preview and a captured AG00 hold produce a reference frame.
SCREEN_TEST_SLOW and STATUS are then pipelined; the device paces the frame at
28,000 bytes/second while the host reads continuously. Host reader throttling
cannot reliably establish device-side backpressure on macOS CDC and is unused.
Only exact RGB565 byte count, exact trailer, and reference SHA-256 validate the
binary body. Pixel bytes are never searched for text. A matching hash does not
prove arbitrary images, physical touch, or execution by the desktop application.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from tab5_transport import open_serial
from verify_device import read_line
from verify_neon import require
from verify_title_scroll import TitleSession, encode, host_fixture, preview_fixtures
from verify_transition_performance import firmware_name, metadata, quiet


ROOT = Path(__file__).resolve().parents[1]
PIXEL_BYTES = 1280 * 720 * 2
TRAILER = b"\nEND_FRAME\n"
DEVICE_PACED_RATE = 28_000
PRESS = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
RELEASE = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 0}}


@dataclass
class FrameReader:
    """Resumable exact-length reader; it never interprets pixel bytes as lines."""
    remaining: int = PIXEL_BYTES
    trailer: bytearray = field(default_factory=bytearray)
    digest: object = field(default_factory=hashlib.sha256)
    received: int = 0

    @property
    def complete(self) -> bool:
        return self.remaining == 0 and bytes(self.trailer) == TRAILER

    def progress(self) -> dict:
        return {"received_bytes": self.received, "remaining_bytes": self.remaining,
                "trailer_bytes": len(self.trailer), "trailer_hex": self.trailer.hex(),
                "complete": self.complete}

    def read(self, connection, deadline: float, *, on_progress=None) -> dict:
        started = time.monotonic()
        notified = started
        if on_progress:
            on_progress(self.progress())
        while not self.complete:
            if time.monotonic() >= deadline:
                raise TimeoutError("Incomplete SCREEN body/trailer")
            if self.remaining:
                chunk = connection.read(min(4096, self.remaining))
                if not chunk:
                    continue
                require(len(chunk) <= self.remaining, "Reader returned bytes beyond the frame body")
                self.digest.update(chunk)
                self.received += len(chunk)
                self.remaining -= len(chunk)
            else:
                chunk = connection.read(len(TRAILER) - len(self.trailer))
                self.trailer.extend(chunk)
                require(TRAILER.startswith(self.trailer), "SCREEN trailer is not exact END_FRAME")
            if on_progress and (self.complete or time.monotonic() - notified >= 10):
                on_progress(self.progress())
                notified = time.monotonic()
        return {"bytes": self.received, "sha256": self.digest.hexdigest(),
                "trailer": "\\nEND_FRAME\\n", "elapsed_seconds": round(time.monotonic() - started, 3)}


class StreamSession(TitleSession):
    def __init__(self, connection, timeout):
        super().__init__(connection, timeout)
        self.reader: FrameReader | None = None
        self.request_pending = False
        self.capture_activity = time.monotonic()

    def command(self, value: str, expected: str) -> None:
        try:
            super().command(value, expected)
        finally:
            # An ACK failure may still mean the firmware accepted a keepalive.
            if value.startswith(("INPUT_TEST_BEGIN", "INPUT_TOUCH ", "PREVIEW_LIGHTS ",
                                 "PREVIEW_HOST ", "FX ")):
                self.capture_activity = time.monotonic()

    def frame(self, *, slow: bool = False, pipeline_status: bool = False, on_progress=None) -> dict:
        require(not self.request_pending, "Previous binary response has not been recovered")
        self.request_pending = True
        self.reader = None
        command = "SCREEN_TEST_SLOW" if slow else "SCREEN"
        self.send(command + ("\nSTATUS" if pipeline_status else ""))
        deadline = time.monotonic() + (90 if slow else 30)
        header = self.line(deadline)
        require(re.fullmatch(rb"FRAME 1280 720 RGB565_BE", header) is not None,
                "Unexpected SCREEN header")
        self.reader = FrameReader()
        result = self.reader.read(self.connection, deadline, on_progress=on_progress)
        self.request_pending = False
        return result

    def reject_slow_without_capture(self) -> None:
        # Expected rejection bypasses TitleSession.line's generic ERROR check.
        # Keep recovery armed if a buggy firmware starts a binary frame instead.
        self.request_pending = True
        self.reader = None
        self.send("SCREEN_TEST_SLOW")
        raw = read_line(self.connection, time.monotonic() + 10)
        if raw == b"SCREEN_TEST_ERROR":
            self.request_pending = False
            return
        if raw == b"FRAME 1280 720 RGB565_BE":
            self.reader = FrameReader()
        raise RuntimeError("SCREEN_TEST_SLOW was not explicitly rejected outside capture")

    def queued_status(self, deadline: float) -> dict:
        require(not self.request_pending, "Refusing text parsing inside a binary frame")
        while time.monotonic() < deadline:
            line = self.line(deadline)
            if line.startswith(b"{"):
                value = json.loads(line)
                if isinstance(value, dict) and "firmware" in value:
                    return value
            require(not line, "Unexpected text before queued STATUS")
        raise TimeoutError("No pipelined STATUS after END_FRAME")

    def recover_stream(self, capture_started: float) -> dict:
        """Keep the serial lock through exact recovery or watchdog quarantine."""
        if not self.request_pending:
            return {"needed": False}
        if self.reader is not None:
            try:
                self.reader.read(self.connection, time.monotonic() + 20)
                self.request_pending = False
                return {"needed": True, "exact_frame_recovered": True}
            except (RuntimeError, TimeoutError):
                pass
        # A truncated/aborted frame has no trustworthy textual boundary. Keep
        # draining under the lock, without sending any command, until the
        # firmware's 30s no-progress and 60s capture watchdogs have both elapsed.
        started = last_byte = time.monotonic()
        deadline = max(started + 80, capture_started + 70)
        while time.monotonic() < deadline:
            data = self.connection.read(4096)
            now = time.monotonic()
            if data:
                last_byte = now
            if now - last_byte >= 35 and now - capture_started >= 65:
                self.reader = None
                self.request_pending = False
                current = self.status()
                require(current.get("diagnostic_frame_active") is False
                        and current.get("input_capture") is False,
                        "Watchdog quarantine did not close the frame/capture gates")
                return {"needed": True, "exact_frame_recovered": False,
                        "watchdog_quarantine_confirmed": True}
        raise TimeoutError("Binary recovery could not establish a quiet watchdog boundary")


def safe_status(value: dict) -> dict:
    result = metadata(value)
    for key in ("diagnostic_frame_active", "diagnostic_lines_pending", "diagnostic_lines_dropped"):
        if key in value:
            result[key] = value[key]
    return result


def check_host_counter(current: dict, baseline: dict) -> None:
    require(current["firmware"] == baseline["firmware"], "Firmware changed during stream QA")
    require(current["inputs_tx"] == baseline["inputs_tx"], "Native host input counter changed")


def verify(output: Path, port: str | None = None, firmware: str = "0.11.1",
           timeout: float = 10) -> dict:
    require(timeout > 0, "Timeout must be positive")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"firmware": firmware_name(firmware), "success": False,
              "scope": "Exact frozen-reference bytes and deferred captured release across one "
                       "watchdog-expiring SCREEN stream; not general pixel correctness or desktop execution",
              "device_paced_rate": DEVICE_PACED_RATE,
              "expected_body_seconds": round(PIXEL_BYTES / DEVICE_PACED_RATE, 3),
              "host_read_throttling": False, "frame_deadline_seconds": 90,
              "frame_bytes": PIXEL_BYTES, "synthetic_preview_only": True}

    def checkpoint(stage: str, progress: dict | None = None) -> None:
        report["stage"] = stage
        if progress is not None:
            report["frame_progress"] = progress
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(output)
        print(json.dumps({"stage": stage, **(progress or {})}), file=sys.stderr, flush=True)
    failure: BaseException | None = None
    with open_serial(port, timeout) as connection:
        connection.timeout = .1
        # Only the deliberately paced frame/queued response need a 90s window.
        # Ordinary ACKs and cleanup STATUS keep the bounded caller timeout.
        session = StreamSession(connection, timeout)
        baseline = session.status()
        require(baseline["firmware"] == report["firmware"], f"Expected {report['firmware']}")
        require(not baseline["input_capture"] and not baseline["lighting_preview"],
                "Another capture/preview is active")
        require(baseline.get("playing") is False and baseline["settings_open"] is False,
                "Open the MICRO control page and close settings before stream QA")
        require(baseline.get("diagnostic_frame_active") is False,
                "Firmware does not expose an idle SCREEN gate")
        report["before"] = safe_status(baseline)
        attempted = False
        capture_started = time.monotonic()
        try:
            checkpoint("outside_capture_rejection")
            session.reject_slow_without_capture()
            report["slow_command_capture_gate"] = "rejected outside capture"
            checkpoint("captured_preview_setup")
            attempted = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            titles = tuple(f"Stream preview {index + 1}" for index in range(6))
            session.command("PREVIEW_LIGHTS " + encode(preview_fixtures(titles)), "PREVIEW_LIGHTS_OK")
            session.command("PREVIEW_HOST " + encode(host_fixture("Synthetic stream preview")),
                            "PREVIEW_HOST_OK")
            session.command("FX FREEZE", "FX FREEZE")
            session.command("INPUT_TOUCH 125 151 1", "INPUT_TOUCH_OK")
            quiet(1.5)
            held = session.status()
            check_host_counter(held, baseline)
            require(held["input_capture"] and held["lighting_preview"] and held["effects_frozen"],
                    "Frozen captured preview was not established")
            require(session.events == [PRESS], "Expected exactly one captured AG00 press")
            checkpoint("reference_frame")
            report["reference"] = session.frame(
                on_progress=lambda value: checkpoint("reference_frame", value))
            require(session.events == [PRESS], "Hold released before the slow-frame test")
            # Renew the capture timer immediately before the long frame. Do not
            # send keepalive commands until after its watchdog has fired.
            session.command("FX FREEZE", "FX FREEZE")
            capture_started = time.monotonic()
            status_deadline = capture_started + 90
            checkpoint("device_paced_frame")
            report["slow_frame"] = session.frame(slow=True, pipeline_status=True,
                on_progress=lambda value: checkpoint("device_paced_frame", value))
            require(session.events == [PRESS], "Text event was parsed before the complete frame trailer")
            checkpoint("queued_status_after_trailer")
            after = session.queued_status(status_deadline)
            report["after_watchdog"] = safe_status(after)
            check_host_counter(after, baseline)
            require(report["slow_frame"]["sha256"] == report["reference"]["sha256"],
                    "Slow SCREEN bytes differ from the frozen reference")
            require(report["slow_frame"]["elapsed_seconds"] >= 60,
                    "Transfer did not span the capture watchdog interval")
            require(after["input_capture"] is False and after["lighting_preview"] is False
                    and after["diagnostic_frame_active"] is False,
                    "Queued STATUS did not confirm watchdog/frame cleanup after the device-paced transfer")
            require(session.events == [PRESS, RELEASE], "Expected one deferred captured AG00 release")
            require(after["captured_inputs"] - baseline["captured_inputs"] == 2,
                    "Captured input counter did not match press/release")
            require(after.get("diagnostic_lines_pending") == 0
                    and after.get("diagnostic_lines_dropped") == baseline.get("diagnostic_lines_dropped"),
                    "Deferred diagnostic lines remained pending or were dropped")
            require(after["effects_frozen"] == baseline["effects_frozen"],
                    "Watchdog did not restore the original effects setting")
        except BaseException as error:
            failure = error
            report["error"] = f"{type(error).__name__}: {error}"
            if session.reader is not None:
                report["frame_progress_at_failure"] = session.reader.progress()
        finally:
            cleanup_errors = []
            safe_for_text = True
            try:
                checkpoint("stream_recovery")
                report["stream_recovery"] = session.recover_stream(session.capture_activity)
            except Exception as error:
                safe_for_text = False
                cleanup_errors.append(f"Stream recovery: {error}")
            if safe_for_text:
                try:
                    checkpoint("capture_cleanup")
                    current = session.status()
                    if attempted and current.get("input_capture") is True:
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                    final = session.status()
                    check_host_counter(final, baseline)
                    require(not final["input_capture"] and not final["lighting_preview"]
                            and not final["diagnostic_frame_active"], "Diagnostic gates were not cleared")
                    require(final["theme"] == baseline["theme"] and final["language"] == baseline["language"]
                            and final["effects_frozen"] == baseline["effects_frozen"],
                            "Original UI preferences were not preserved")
                    report["after"] = safe_status(final)
                except Exception as error:
                    cleanup_errors.append(f"Owned capture cleanup: {error}")
            report["captured_events"] = session.events
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                if failure is None:
                    failure = RuntimeError("; ".join(cleanup_errors))
            report["success"] = failure is None
            checkpoint("complete" if failure is None else "failed")
    if failure:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        raise RuntimeError(f"SCREEN stream QA failed; evidence: {output}: {failure}") from failure
    return {"success": True, "path": str(output), "sha256": report["slow_frame"]["sha256"],
            "read_seconds": report["slow_frame"]["elapsed_seconds"],
            "captured_event_count": len(session.events), "native_input_delta": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "artifacts/screenshot-stream-0.11.1.json")
    parser.add_argument("--port")
    parser.add_argument("--firmware", default="0.11.1")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    try:
        result = verify(args.output, args.port, args.firmware, args.timeout)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
