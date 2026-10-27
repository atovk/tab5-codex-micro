#!/usr/bin/env python3
"""Verify Tab5 landscape mapping and captured input without desktop actions.

Uses only CDC diagnostics. Forced rotations are gated by INPUT_TEST capture;
the real IMU recovery check runs after capture ends, when gravity is suitable.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import time

from tab5_transport import open_serial
from verify_device import read_line
from verify_neon import Session, require


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts/orientation-0.10.2.json"
FIRMWARE = "tab5-micro-0.10.2"
RAW_POINTS = ((0, 0), (719, 0), (0, 1279), (719, 1279), (359, 639))
ENCODER = (244, 352)  # Same logical center as verify_controls.py.


def check_safety(current: dict, before: dict, events: list[dict], capture: bool) -> None:
    require(current["inputs_tx"] == before["inputs_tx"], "Desktop input counter changed")
    require(current["input_capture"] is capture, "Unexpected input capture state")
    require(current["captured_inputs"] - before["captured_inputs"] == len(events),
            "Captured input counter does not match the collected events")


def imu_health(current: dict) -> dict:
    imu = current["orientation"]
    require(imu["imu_available"] is True, "IMU is unavailable")
    require(imu["rotation"] in (1, 3), "Orientation is not landscape")
    require(imu["samples"] > 0, "IMU has not produced a sample")
    values = [imu[group][axis] for group in ("accel", "gyro") for axis in ("x", "y", "z")]
    require(all(isinstance(value, (int, float)) and math.isfinite(value) for value in values),
            "IMU reported a non-finite acceleration or gyro value")
    return imu


def gravity_target(imu: dict) -> tuple[int | None, str]:
    """Match the actual poll() gates; flat/portrait/motion is a genuine skip."""
    ax, ay, az = (imu["accel"][axis] for axis in ("x", "y", "z"))
    norm = math.sqrt(ax * ax + ay * ay + az * az)
    if not .75 <= norm <= 1.25:
        return None, f"Acceleration norm {norm:.4f} is outside 0.75..1.25g"
    if abs(ax) < .55 or abs(ax) <= 1.2 * abs(ay):
        return None, "Flat or portrait posture does not provide a landscape gravity target"
    if any(abs(imu["gyro"][axis]) > 45 for axis in ("x", "y", "z")):
        return None, "Device is moving faster than the 45deg/s gyro gate"
    return (1 if ax < 0 else 3), "Stable landscape gravity projection"


def map_point(session: Session, raw: tuple[int, int]) -> tuple[int, int]:
    session.send(f"ORIENTATION_MAP {raw[0]} {raw[1]}")
    deadline = time.monotonic() + session.timeout
    while time.monotonic() < deadline:
        match = re.fullmatch(rb"ORIENTATION_MAP (-?\d+) (-?\d+)", session.line(deadline))
        if match:
            point = tuple(map(int, match.groups()))
            require(0 <= point[0] < 1280 and 0 <= point[1] < 720,
                    f"Mapped point is outside the logical display: {raw} -> {point}")
            return point
    raise TimeoutError("No ORIENTATION_MAP response")


def mapping_formula(points: list[tuple[int, int]]) -> str:
    # Determine both axis directions from device output, rather than assuming
    # a panel-specific raw-touch orientation/calibration offset.
    matches = []
    for reverse_x in (False, True):
        for reverse_y in (False, True):
            expected = [(1279 - y if reverse_x else y, 719 - x if reverse_y else x)
                        for x, y in RAW_POINTS]
            if points == expected:
                matches.append("x=" + ("1279-raw_y" if reverse_x else "raw_y")
                               + ", y=" + ("719-raw_x" if reverse_y else "raw_x"))
    require(len(matches) == 1, f"Mapping is not one consistent axis-swapped formula: {points}")
    return matches[0]


def expect_held_rejection(session: Session, target: int) -> str:
    # Session.line deliberately raises on ERROR; this ERROR is the assertion.
    # Continue collecting asynchronous capture events while reading it directly.
    session.send(f"ORIENTATION_TEST {target}")
    deadline = time.monotonic() + session.timeout
    while time.monotonic() < deadline:
        raw = read_line(session.connection, deadline)
        if raw.startswith(b"INPUT_EVENT "):
            session.events.append(json.loads(raw.removeprefix(b"INPUT_EVENT ")))
        elif raw == b"ORIENTATION_TEST_ERROR":
            return raw.decode()
        elif raw == b"ORIENTATION_TEST_OK":
            raise RuntimeError("Orientation changed while a synthetic touch was held")
        elif b"ERROR" in raw:
            raise RuntimeError(raw.decode("utf-8", errors="replace"))
    raise TimeoutError("Held ORIENTATION_TEST was not rejected")


def verify(port: str | None = None, timeout: float = 10) -> dict:
    report = {"firmware": FIRMWARE, "success": False, "rotations": {}, "checks": {}}
    error = None
    with open_serial(port, timeout) as connection:
        session = Session(connection, timeout)
        before = session.status()
        report["before"] = before
        began = False
        recovery = {"status": "not_tested"}
        end_at = None
        try:
            require(before["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
            require(before["input_capture"] is False, "Another input capture session is active")
            require(not before["settings_open"], "Close Settings before orientation QA")
            require(not before["lighting_preview"], "Another lighting preview is active")
            imu_health(before)
            time.sleep(.25)
            baseline = session.status()
            imu = imu_health(baseline)
            require(imu["samples"] > before["orientation"]["samples"],
                    "Unblocked IMU sampling did not advance before capture")
            require(imu["failures"] == before["orientation"]["failures"],
                    "IMU reads failed before capture")
            target, reason = gravity_target(imu)
            report["imu_baseline"] = imu
            recovery = {"status": "pending" if target is not None else "skipped",
                        "reason": reason, "target": target}

            # A timed-out BEGIN may still have enabled capture; finally checks
            # ownership via STATUS before issuing the neutralizing END.
            began = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            captured = session.status()
            check_safety(captured, before, session.events, True)
            require(not session.events, "Unexpected input while entering capture")
            report["capture_started"] = captured
            # BEGIN rejects an active game. Keep even local navigation behind
            # capture so it cannot cancel a real desktop gesture during setup.
            session.command("MICRO", "MICRO")

            mappings = {}
            for rotation in (1, 3):
                session.command(f"ORIENTATION_TEST {rotation}", "ORIENTATION_TEST_OK")
                current = session.status()
                check_safety(current, before, session.events, True)
                require(current["orientation"]["rotation"] == rotation, "Forced rotation was not applied")
                require((current["display_width"], current["display_height"]) == (1280, 720),
                        "Landscape logical dimensions changed")
                points = [map_point(session, raw) for raw in RAW_POINTS]
                require(set(points[:4]) == {(0, 0), (1279, 0), (0, 719), (1279, 719)},
                        "Raw corners do not cover all logical corners")
                mappings[rotation] = points
                start = len(session.events)
                session.touch(*ENCODER, True)
                rejected = expect_held_rejection(session, 4 - rotation)
                held = session.status()
                require(held["orientation"]["rotation"] == rotation,
                        "Rejected rotation still changed the display")
                check_safety(held, before, session.events, True)
                session.touch(*ENCODER, False)
                expected = [{"m": "v.oai.hid", "p": {"k": "ENC", "act": action}}
                            for action in (1, 0)]
                require(session.events[start:] == expected, "Encoder edges were not exactly press/release")
                # The same request must become legal immediately after release.
                session.command(f"ORIENTATION_TEST {4 - rotation}", "ORIENTATION_TEST_OK")
                released = session.status()
                require(released["orientation"]["rotation"] == 4 - rotation,
                        "Rotation remained blocked after releasing the synthetic touch")
                check_safety(released, before, session.events, True)
                report["rotations"][str(rotation)] = {
                    "logical_size": [current["display_width"], current["display_height"]],
                    "mapping_formula": mapping_formula(points),
                    "points": [{"raw": raw, "logical": point} for raw, point in zip(RAW_POINTS, points)],
                    "held_rejection": rejected, "captured_events": session.events[start:],
                }
            for one, three in zip(mappings[1], mappings[3]):
                require((one[0] + three[0], one[1] + three[1]) == (1279, 719),
                        "Rotation 1/3 mappings are not complementary")
            report["checks"]["complementary_mapping"] = True

            # Capture blocks I2C polling, so no sample-growth assertion belongs
            # here. Force the wrong direction only when real gravity can recover.
            final_rotation = 4 - target if target is not None else imu["rotation"]
            session.command(f"ORIENTATION_TEST {final_rotation}", "ORIENTATION_TEST_OK")
            final_capture = session.status()
            check_safety(final_capture, before, session.events, True)
            recovery["forced_rotation"] = final_rotation
            recovery["changes_before_end"] = final_capture["orientation"]["changes"]
            report["capture_finished"] = final_capture
        except Exception as failure:
            error = failure
            report["error"] = f"{type(failure).__name__}: {failure}"
        finally:
            cleanup_errors = []
            if began:
                try:
                    if session.status().get("input_capture") is True:
                        # Firmware END releases any failed synthetic hold before
                        # disabling capture; it never forwards that edge to HID.
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                        end_at = time.monotonic()
                except Exception as failure:
                    cleanup_errors.append(f"INPUT_TEST_END: {failure}")
            try:
                time.sleep(.9)
                query_at = time.monotonic()
                after = session.status()
                report["after"] = after
                check_safety(after, before, session.events, False)
                after_imu = imu_health(after)
                resume_baseline = report.get("capture_finished", report.get("capture_started", before))
                require(after_imu["samples"] > resume_baseline["orientation"]["samples"],
                        "IMU sampling did not resume after capture")
                require(after_imu["blocked"] is False, "IMU polling remains blocked after capture")
                require(after_imu["failures"] == before["orientation"]["failures"],
                        "IMU read failures increased during QA")
                require(after["theme"] == before["theme"]
                        and after["brightness_percent"] == before["brightness_percent"]
                        and after["audio"]["enabled"] == before["audio"]["enabled"]
                        and after["audio"]["volume"] == before["audio"]["volume"],
                        "Local preferences changed during orientation QA")
                require(after["effects_frozen"] == before["effects_frozen"],
                        "Capture did not restore the original animation setting")
                if recovery["status"] == "pending" and error is None:
                    require(after_imu["rotation"] == recovery["target"]
                            and after_imu["changes"] > recovery["changes_before_end"]
                            and after_imu["blocked"] is False,
                            "Real IMU did not automatically recover the gravity orientation")
                    recovery["status"] = "passed"
                recovery["wait_after_end_seconds"] = query_at - end_at if end_at is not None else None
                # A bulk STATUS read is not a touch/render latency measurement.
                recovery["status_query_seconds"] = time.monotonic() - query_at
            except Exception as failure:
                cleanup_errors.append(str(failure))
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                if error is None:
                    error = RuntimeError("; ".join(cleanup_errors))
    report["automatic_recovery"] = recovery
    report["captured_events"] = session.events
    report["success"] = error is None
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if error is not None:
        raise RuntimeError(f"Orientation QA failed; see {OUTPUT}: {error}") from error
    return {"path": str(OUTPUT), "success": True, "automatic_recovery": recovery["status"],
            "host_inputs_before": before["inputs_tx"], "host_inputs_after": report["after"]["inputs_tx"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    print(json.dumps(verify(args.port, args.timeout), ensure_ascii=False, indent=2))
