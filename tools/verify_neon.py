#!/usr/bin/env python3
"""Verify 0.7 neon states and touch priority using a gated on-device demo.

All lighting fixtures are synthetic previews, not evidence of live chat states.
The only native input exercised is one captured encoder press/release. No HID
handle is opened, and inputs_tx must remain unchanged throughout the test.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

from PIL import Image, ImageChops, ImageDraw

from tab5_transport import open_serial
from verify_device import read_line, rgb565_image


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = "tab5-micro-0.11.0"
FIXTURES = [
    {"c": 0x304FFE, "b": 1.0, "e": 1, "s": 0.0},  # Running, steady.
    {"c": 0xFFFFFF, "b": 1.0, "e": 4, "s": 0.4},  # Idle, selected/pulsing.
    {"c": 0x00FF4C, "b": 1.0, "e": 1, "s": 0.0},  # Unread.
    {"c": 0xFF6D00, "b": 1.0, "e": 4, "s": 0.4},  # Approval OR input.
    {"c": 0xFF0033, "b": 1.0, "e": 1, "s": 0.0},  # Error.
    {"c": 0x304FFE, "b": 1.0, "e": 6, "s": 0.4},  # SDK effect, unknown.
]
LABELS = ["运行中", "待命", "有更新", "待处理", "异常", "状态灯"]
CARD_X = [28 + 206 * index for index in range(6)]
CARD_Y, CARD_WIDTH, CARD_HEIGHT = 110, 194, 82
# The renderer presents these disjoint border patches. Text crops below are
# strictly inside them and exclude the separate native-color LED at x+175.
EDGE_PATCHES = [
    (0, 0, 24, 24), (24, 0, 146, 8), (170, 0, 24, 24),
    (185, 24, 9, 34), (170, 58, 24, 24), (24, 74, 146, 8),
    (0, 58, 24, 24), (0, 24, 9, 34),
]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class Session:
    def __init__(self, connection, timeout: float):
        self.connection = connection
        self.timeout = timeout
        self.events: list[dict] = []
        self.frame_remaining: int | None = None
        self.frame_trailer_pending = False

    def send(self, value: str) -> None:
        payload = (value + "\n").encode("utf-8")
        require(len(payload) <= 6144, "Diagnostic command exceeds firmware limit")
        self.connection.write(payload)
        self.connection.flush()

    def line(self, deadline: float) -> bytes:
        while True:
            raw = read_line(self.connection, deadline)
            if raw.startswith(b"INPUT_EVENT "):
                self.events.append(json.loads(raw.removeprefix(b"INPUT_EVENT ")))
                continue
            if b"ERROR" in raw:
                raise RuntimeError(raw.decode("utf-8", errors="replace"))
            return raw

    def command(self, value: str, expected: str) -> None:
        self.send(value)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.line(deadline).decode("utf-8", errors="replace") == expected:
                return
        raise TimeoutError(f"No acknowledgement for {value.split(' ', 1)[0]}")

    def status(self) -> dict:
        self.send("STATUS")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self.line(deadline)
            if raw.startswith(b"{"):
                value = json.loads(raw)
                if isinstance(value, dict) and "firmware" in value:
                    return value
        raise TimeoutError("No device STATUS response")

    def preview(self, fixtures: list[dict]) -> None:
        self.command("PREVIEW_LIGHTS " + json.dumps(fixtures, separators=(",", ":")),
                     "PREVIEW_LIGHTS_OK")

    def screenshot(self, path: Path) -> dict:
        self.send("SCREEN")
        deadline = time.monotonic() + max(30, self.timeout)
        while True:
            match = re.fullmatch(rb"FRAME (\d+) (\d+) RGB565_BE", self.line(deadline))
            if match:
                width, height = map(int, match.groups())
                break
        require((width, height) == (1280, 720),
                f"Unexpected framebuffer dimensions: {width}x{height}")
        self.frame_remaining = width * height * 2
        self.frame_trailer_pending = True
        pixels = bytearray()
        while self.frame_remaining and time.monotonic() < deadline:
            chunk = self.connection.read(min(65536, self.frame_remaining))
            pixels.extend(chunk)
            self.frame_remaining -= len(chunk)
        require(self.frame_remaining == 0,
                f"Incomplete framebuffer: {len(pixels)}/{width * height * 2} bytes")
        trailer = b""
        for _ in range(4):
            trailer = self.line(deadline)
            if trailer:
                break
        require(trailer == b"END_FRAME", f"Invalid framebuffer trailer: {trailer!r}")
        self.frame_trailer_pending = False
        self.frame_remaining = None
        path.parent.mkdir(parents=True, exist_ok=True)
        rgb565_image(pixels, width, height).save(path, format="PNG")
        return {"path": str(path.resolve()), "width": width, "height": height,
                "synthetic_preview": True}

    def recover_frame(self) -> None:
        """Finish a partially consumed frame before issuing textual commands."""
        if self.frame_remaining is None:
            return
        # A shot normally has a 30s deadline. Keep recovery bounded to leave
        # room for END before the firmware's 60s capture watchdog expires.
        deadline = time.monotonic() + 15
        while self.frame_remaining and time.monotonic() < deadline:
            chunk = self.connection.read(min(65536, self.frame_remaining))
            self.frame_remaining -= len(chunk)
        require(self.frame_remaining == 0, "Could not drain the partial framebuffer")
        if self.frame_trailer_pending:
            while time.monotonic() < deadline:
                if self.line(deadline) == b"END_FRAME":
                    self.frame_trailer_pending = False
                    break
            require(not self.frame_trailer_pending, "Could not find the framebuffer trailer")
        self.frame_remaining = None

    def touch(self, x: int, y: int, down: bool) -> None:
        require(self.status().get("input_capture") is True,
                "Refusing diagnostic touch outside input capture mode")
        self.command(f"INPUT_TOUCH {x} {y} {int(down)}", "INPUT_TOUCH_OK")


def changed_pixels(image: Image.Image) -> int:
    return sum(pixel != (0, 0, 0) for pixel in image.convert("RGB").get_flattened_data())


def compare_live(first: Path, second: Path, difference_path: Path) -> dict:
    with Image.open(first) as a, Image.open(second) as b:
        difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    text_changes = []
    edge_changes = []
    allowed = Image.new("L", difference.size, 0)
    mask = ImageDraw.Draw(allowed)
    for index, x in enumerate(CARD_X):
        # Badge/number and title are checked independently, without OCR.
        crops = [(x + 16, CARD_Y + 9, x + 169, CARD_Y + 30),
                 (x + 16, CARD_Y + 34, x + 178, CARD_Y + 72)]
        text_changes.append(sum(changed_pixels(difference.crop(box)) for box in crops))
        edge_changes.append(changed_pixels(difference.crop(
            (x, CARD_Y, x + CARD_WIDTH, CARD_Y + CARD_HEIGHT))))
        for px, py, width, height in EDGE_PATCHES:
            mask.rectangle((x + px, CARD_Y + py, x + px + width - 1,
                            CARD_Y + py + height - 1), fill=255)
    outside = Image.composite(Image.new("RGB", difference.size), difference, allowed)
    difference.save(difference_path)
    require(not any(text_changes), f"Live animation changed text pixels: {text_changes}")
    require(changed_pixels(outside) == 0,
            "Live animation changed pixels outside the six border damage regions")
    return {"changed_card_pixels": edge_changes, "changed_text_pixels": text_changes,
            "changed_outside_border_pixels": 0,
            "difference_path": str(difference_path.resolve())}


def check_safety(current: dict, baseline: dict, events: list[dict],
                 capture: bool = True) -> None:
    require(current["inputs_tx"] == baseline["inputs_tx"],
            "Native host input counter changed during gated QA")
    require(current["input_capture"] is capture, "Input capture state is unexpected")
    delta = current["captured_inputs"] - baseline["captured_inputs"]
    require(delta == len(events),
            f"Captured input count/list mismatch: {delta} vs {len(events)}")


def check_fixture_status(current: dict, fixtures: list[dict], labels: list[str]) -> None:
    require(current.get("lighting_preview") is True, "Lighting preview is inactive")
    require(len(current["slots"]) == 6, "Expected six visible slot states")
    for index, (slot, fixture, label) in enumerate(zip(current["slots"], fixtures, labels)):
        require(slot["c"] == fixture["c"] and slot["e"] == fixture["e"]
                and slot["b"] == round(fixture["b"] * 255) and slot["label"] == label,
                f"Slot {index} fixture/status mismatch: {slot}")


def verify(output: Path, port: str | None = None, timeout: float = 10,
           stress_seconds: float = 3, max_render_us: int | None = None,
           max_input_us: int | None = None) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    directory = output.parent
    report: dict = {"firmware": FIRMWARE, "synthetic_preview_only": True,
                    "fixtures": FIXTURES, "fixture_labels": LABELS,
                    "captures": [], "checks": {}, "success": False}
    error: Exception | None = None
    with open_serial(port, timeout) as connection:
        session = Session(connection, timeout)
        before = session.status()
        require(before["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not before["input_capture"], "Another input capture session is active")
        require(not before["lighting_preview"], "Another lighting preview is active")
        require(before["theme"] in ("dark", "light"), "Unrecognized starting theme")
        report["before"] = before
        began = False
        try:
            # Even an ACK timeout may leave capture enabled; always attempt END.
            began = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            started = session.status()
            check_safety(started, before, session.events)
            require(started["effects_frozen"] is True, "Capture did not freeze effects")
            require(not session.events, "Unexpected input while entering capture mode")

            for theme in ("dark", "light"):
                session.command("FX FREEZE", "FX FREEZE")
                session.preview(FIXTURES)
                session.command(f"THEME {theme.upper()}", f"THEME {theme.upper()}")
                time.sleep(.5)
                current = session.status()
                check_safety(current, before, session.events)
                check_fixture_status(current, FIXTURES, LABELS)
                require(current["effects_frozen"] is True, "Demo is not frozen")
                require(current["theme"] == theme, "Theme command was not applied")
                initial_frames = current["ui_profile"]["neon_frames"]
                time.sleep(.2)
                frozen = session.status()
                require(frozen["ui_profile"]["neon_frames"] == initial_frames,
                        "Frozen animation continued to step")
                capture = session.screenshot(directory / f"tab5-neon-status-demo-{theme}-0.11.0.png")
                report["captures"].append(capture)
                report["checks"][f"frozen_{theme}"] = current

            session.command("THEME DARK", "THEME DARK")
            session.command("FX LIVE", "FX LIVE")
            time.sleep(.4)
            live = None
            for attempt, delay in enumerate((.197, .311, .467), 1):
                # Each command renews the capture watchdog before a framebuffer.
                session.command("FX LIVE", "FX LIVE")
                first = directory / f"tab5-neon-live-demo-{attempt}-a-0.11.0.png"
                second = directory / f"tab5-neon-live-demo-{attempt}-b-0.11.0.png"
                report["captures"].append(session.screenshot(first))
                time.sleep(delay)
                session.command("FX LIVE", "FX LIVE")
                report["captures"].append(session.screenshot(second))
                live = compare_live(first, second, directory /
                                    f"tab5-neon-live-demo-diff-{attempt}-0.11.0.png")
                if live["changed_card_pixels"][0] > 0:
                    break
            require(live is not None and live["changed_card_pixels"][0] > 0,
                    "Running chase border did not change in live snapshots")
            report["checks"]["live_border_and_stable_text"] = live
            current = session.status()
            check_safety(current, before, session.events)
            require(not session.events, "Lighting-only QA unexpectedly generated input")
            report["checks"]["lighting_only_captured_inputs_delta"] = 0

            running = [{"c": 0x304FFE, "b": 1.0, "e": 1, "s": 0.0}
                       for _ in range(6)]
            session.preview(running)
            session.command("FX LIVE", "FX LIVE")
            time.sleep(.25)
            check_fixture_status(session.status(), running, ["运行中"] * 6)
            session.command("PERF_RESET", "OK")
            started_at = time.monotonic()
            # STATUS serializes a large JSON object synchronously over CDC.
            # Keep this interval free of all diagnostic I/O so its first status
            # snapshot measures animation rather than our own polling traffic.
            while time.monotonic() - started_at < stress_seconds:
                time.sleep(max(0, min(.5, stress_seconds - (time.monotonic() - started_at))))
            quiet_duration = time.monotonic() - started_at
            metrics = session.status()
            check_safety(metrics, before, session.events)
            require(metrics["ui_profile"]["neon_frames"] >= 6,
                    "Six-running stress produced too few animation steps")
            require(metrics["ui_profile"]["neon_draw_us"] > 0
                    and metrics["ui_profile"]["neon_present_us"] > 0,
                    "Neon timing profile was not populated")
            if max_render_us is not None:
                require(metrics["render_max_us"] <= max_render_us,
                        "Render time exceeded the requested limit")
            if max_input_us is not None:
                require(metrics["input_interval_max_us"] <= max_input_us,
                        "Input interval exceeded the requested limit")
            report["checks"]["six_running_stress"] = {
                "duration_seconds": quiet_duration, "status": metrics,
                "max_render_limit_us": max_render_us, "max_input_limit_us": max_input_us,
                "note": "No diagnostic I/O after PERF_RESET until this first status snapshot; "
                        "no hard timing limit unless explicitly supplied",
            }

            # A captured encoder hold proves that idle animation yields while
            # touch is active. It produces two local events and no host action.
            session.touch(244, 352, True)
            pressed_at = time.monotonic()
            time.sleep(.15)
            held_start = session.status()
            check_safety(held_start, before, session.events)
            time.sleep(max(0, .65 - (time.monotonic() - pressed_at)))
            held_end = session.status()
            require(held_start["ui_profile"]["neon_frames"] ==
                    held_end["ui_profile"]["neon_frames"],
                    "Neon animation stepped while the encoder was held")
            session.touch(244, 352, False)
            expected = [{"m": "v.oai.hid", "p": {"k": "ENC", "act": action}}
                        for action in (1, 0)]
            require(session.events == expected, f"Unexpected captured events: {session.events}")
            time.sleep(.3)
            resumed = session.status()
            check_safety(resumed, before, session.events)
            require(resumed["ui_profile"]["neon_frames"] >
                    held_end["ui_profile"]["neon_frames"],
                    "Animation did not resume after release")
            report["checks"]["touch_priority"] = {
                "hold_ms_min": 650, "captured_events": expected,
                "held_neon_frames_delta": 0,
                "held_start": held_start, "held_end": held_end,
                "resumed": resumed,
            }
        except Exception as failure:
            error = failure
            report["error"] = f"{type(failure).__name__}: {failure}"
        finally:
            cleanup_errors = []
            stream_ready = True
            if began:
                try:
                    session.recover_frame()
                except Exception as failure:
                    cleanup_errors.append(f"Framebuffer recovery: {failure}")
                    stream_ready = False
            if began and stream_ready:
                # END also neutralizes a hold if a check failed before release,
                # clears preview, and restores the original effects setting.
                for value, expected in (
                    ("PREVIEW_LIGHTS_CLEAR", "PREVIEW_LIGHTS_CLEARED"),
                    (f"THEME {before['theme'].upper()}", f"THEME {before['theme'].upper()}"),
                ):
                    try:
                        session.command(value, expected)
                    except Exception as failure:
                        cleanup_errors.append(f"{value}: {failure}")
                try:
                    # BEGIN can be explicitly rejected before capture starts.
                    # An unguarded END would then release a real desktop key.
                    if session.status().get("input_capture") is True:
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                except Exception as failure:
                    cleanup_errors.append(f"INPUT_TEST_END: {failure}")
            if stream_ready:
                try:
                    after = session.status()
                    report["after"] = after
                    check_safety(after, before, session.events, capture=False)
                    require(not after["lighting_preview"], "Lighting preview was not cleared")
                    require(after["effects_frozen"] == before["effects_frozen"],
                            "Original effects setting was not restored")
                    require(after["theme"] == before["theme"], "Original theme was not restored")
                except Exception as failure:
                    cleanup_errors.append(str(failure))
            else:
                cleanup_errors.append(
                    "Text commands skipped because the binary stream could not be recovered; "
                    "firmware capture watchdog must perform cleanup")
            report["captured_events"] = session.events
            final_captured = report.get("after", {}).get("captured_inputs")
            report["captured_inputs_delta"] = (None if final_captured is None else
                                                final_captured - before["captured_inputs"])
            report["captured_event_count"] = len(session.events)
            report["host_inputs_before"] = before["inputs_tx"]
            report["host_inputs_after"] = report.get("after", {}).get("inputs_tx")
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                if error is None:
                    error = RuntimeError("; ".join(cleanup_errors))
            report["success"] = error is None
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if error is not None:
        raise RuntimeError(f"Neon QA failed; evidence: {output}: {error}") from error
    return {"path": str(output), "success": True,
            "synthetic_preview_only": True, "captured_inputs_delta": len(session.events),
            "host_inputs_before": before["inputs_tx"],
            "host_inputs_after": report["after"]["inputs_tx"],
            "captures": [item["path"] for item in report["captures"]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?",
                        default=ROOT / "artifacts/neon-qa-0.11.0.json")
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--stress-seconds", type=float, default=3)
    parser.add_argument("--max-render-us", type=int)
    parser.add_argument("--max-input-us", type=int)
    args = parser.parse_args()
    if args.timeout <= 0 or not 1 <= args.stress_seconds <= 20:
        parser.error("timeout must be positive and stress-seconds must be 1..20")
    if any(value is not None and value <= 0
           for value in (args.max_render_us, args.max_input_us)):
        parser.error("Timing limits must be positive")
    try:
        result = verify(args.output, args.port, args.timeout, args.stress_seconds,
                        args.max_render_us, args.max_input_us)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
