#!/usr/bin/env python3
"""Measure controls while explicit local test sounds use the real audio worker.

Native control notifications remain captured locally. Measurement loops issue
only INPUT_TOUCH/SOUND_TEST and consume short ACKs; STATUS is read once after
each loop and settle interval. No screenshot or restart is performed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

from tab5_transport import open_serial
from verify_feedback import FeedbackSession, check_safety, wait_saved
from verify_neon import require


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = "tab5-micro-0.10.2"
TEST_VOLUME = 22


def fast_touch(session: FeedbackSession, x: int, y: int, down: bool) -> None:
    # The phase confirms capture before entering this loop. The firmware also
    # refuses INPUT_TOUCH outside capture. Do not call Session.touch here: its
    # defensive STATUS poll would contaminate the interval being measured.
    session.command(f"INPUT_TOUCH {x} {y} {int(down)}", "INPUT_TOUCH_OK")


def test_sound(session: FeedbackSession, cue: str) -> None:
    # Likewise, avoid FeedbackSession.sound_test's preflight STATUS per cue.
    session.command(f"SOUND_TEST {cue}", "SOUND_TEST_OK")


def audio_delta(before: dict, after: dict) -> dict:
    return {key: after[key] - before[key]
            for key in ("queued", "played", "released", "dropped", "suppressed")}


def run_phase(session: FeedbackSession, baseline: dict, name: str) -> dict:
    before = session.status()
    check_safety(session, before, baseline)
    require(before["audio"]["available"] is True
            and before["audio"]["enabled"] is True
            and before["audio"]["volume"] == TEST_VOLUME,
            "Audio is not available at the requested test volume")
    require(before["settings_open"] is False, "Settings cover the control surface")
    start = len(session.events)
    # Finish the prior STATUS write and any preceding control's neutral render
    # before resetting metrics. Nothing else polls STATUS until after the loop.
    time.sleep(.2)
    session.command("PERF_RESET", "OK")
    started_at = time.monotonic()
    sounds = 0
    if name == "dial":
        for step in range(25):
            angle = math.tau * step / 24
            fast_touch(session, round(244 + 96 * math.cos(angle)),
                       round(352 + 96 * math.sin(angle)), True)
            if step and step % 3 == 0:
                test_sound(session, "CW")
                sounds += 1
        fast_touch(session, 340, 352, False)
    elif name == "joystick":
        fast_touch(session, 802, 352, True)
        for step in range(25):
            angle = math.tau * step / 24
            fast_touch(session, round(802 + 80 * math.cos(angle)),
                       round(352 + 80 * math.sin(angle)), True)
            if step and step % 3 == 0:
                test_sound(session, "DIRECTION")
                sounds += 1
        fast_touch(session, 882, 352, False)
    else:
        raise ValueError(f"Unknown control phase: {name}")
    loop_seconds = time.monotonic() - started_at
    time.sleep(.2)
    # First and only diagnostic snapshot after PERF_RESET. Its own JSON write
    # happens after the metrics have been copied into the response document.
    after = session.status()
    check_safety(session, after, baseline)
    events = session.events[start:]
    if name == "dial":
        expected = {"m": "v.oai.hid", "p": {"k": "ENC_CW", "act": 2}}
        require(len(events) == 24 and all(event == expected for event in events),
                f"Expected 24 captured clockwise detents, got {events}")
    else:
        require(len(events) >= 6 and all(event.get("m") == "v.oai.rad" for event in events),
                f"Unexpected captured joystick events: {events}")
        for event in events:
            angle, distance = event["p"]["a"], event["p"]["d"]
            require(math.isfinite(angle) and math.isfinite(distance)
                    and 0 <= angle <= 1 and 0 <= distance <= 1,
                    f"Malformed joystick sample: {event}")
        require(events[0]["p"]["d"] == 0 and events[-1]["p"]["d"] == 0,
                "Joystick did not enter and leave at neutral")
        # The real engine coalesces displacement samples to 20ms, while
        # forcing sector edges. Sample count is intentionally not point count.
        sectors = {int((event["p"]["a"] + .125) * 4) % 4
                   for event in events if event["p"]["d"] >= .5}
        require(sectors == {0, 1, 2, 3}, "Joystick circle missed a direction")
    delta = audio_delta(before["audio"], after["audio"])
    require(delta["played"] > 0, f"No real audio worker playback overlapped {name}")
    require(delta["queued"] > 0 and delta["released"] > 0,
            f"Audio submissions/retirement were absent during {name}")
    return {
        "control": name, "loop_seconds": loop_seconds,
        "explicit_sound_tests": sounds, "captured_events": events,
        "captured_event_count": len(events), "audio_delta": delta,
        "audio_before": before["audio"], "audio_after": after["audio"],
        "audio_timing_scope": "enqueue_max_us/start_max_us are worst values since boot",
        "render_max_us": after["render_max_us"],
        "input_interval_max_us": after["input_interval_max_us"],
        "render_samples": after["render_samples"],
        "input_interval_samples": after["input_interval_samples"],
        "ui_profile": after["ui_profile"],
        "status": after,
        "measurement": "ACK-only control/audio loop; first STATUS after release and 200ms settle",
        "drops": "Cooldown and the replaceable one-slot queue may intentionally drop test notes",
    }


def verify(output: Path, port: str | None = None, timeout: float = 10,
           max_render_us: int | None = None, max_input_us: int | None = None) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {"firmware": FIRMWARE, "success": False,
                    "test_volume": TEST_VOLUME, "phases": [],
                    "audio_evidence_limit": "Worker counters do not prove acoustic output"}
    failure: BaseException | None = None
    with open_serial(port, timeout) as connection:
        session = FeedbackSession(connection, timeout)
        initial = session.status()
        require(initial["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not initial["input_capture"] and not initial["lighting_preview"],
                "Another diagnostic capture/preview is active")
        report["before"] = initial
        touched = False
        try:
            # MICRO is a local page command. No synthetic native key is used to
            # navigate, and capture guards all later control/audio exercise.
            session.command("MICRO", "MICRO")
            session.command("SETTINGS CLOSE", "SETTINGS CLOSE")
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            captured = session.status()
            check_safety(session, captured, initial)
            touched = True
            session.command(f"VOLUME {TEST_VOLUME}", f"VOLUME {TEST_VOLUME}")
            session.command("SOUND ON", "SOUND ON")
            for name in ("dial", "joystick"):
                phase = run_phase(session, initial, name)
                report["phases"].append(phase)
                if max_render_us is not None:
                    require(phase["render_max_us"] <= max_render_us,
                            f"{name} render exceeded requested limit")
                if max_input_us is not None:
                    require(phase["input_interval_max_us"] <= max_input_us,
                            f"{name} input interval exceeded requested limit")
        except BaseException as error:
            failure = error
            report["error"] = f"{type(error).__name__}: {error}"
        finally:
            cleanup_errors = []
            try:
                if session.status().get("input_capture") is True:
                    # Release a gesture even if a loop failed midway.
                    fast_touch(session, 0, 0, False)
            except Exception as error:
                cleanup_errors.append(f"Release: {error}")
            if touched:
                for command in (
                    f"VOLUME {initial['audio']['volume']}",
                    "SOUND ON" if initial["audio"]["enabled"] else "SOUND OFF",
                ):
                    try:
                        session.command(command, command)
                    except Exception as error:
                        cleanup_errors.append(f"{command}: {error}")
            try:
                page = "SETTINGS OPEN" if initial["settings_open"] else "SETTINGS CLOSE"
                session.command(page, page)
                if touched:
                    wait_saved(session)
            except Exception as error:
                cleanup_errors.append(f"Page/preferences: {error}")
            try:
                if session.status().get("input_capture") is True:
                    session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                final = session.status()
                report["after"] = final
                check_safety(session, final, initial, capture=False)
                require(final["audio"]["volume"] == initial["audio"]["volume"]
                        and final["audio"]["enabled"] == initial["audio"]["enabled"],
                        "Original sound preferences were not restored")
                require(final["brightness_percent"] == initial["brightness_percent"],
                        "Audio test changed display brightness")
                require(final["effects_frozen"] == initial["effects_frozen"],
                        "Original effects state was not restored")
            except Exception as error:
                cleanup_errors.append(f"END/restoration: {error}")
            report["captured_events"] = session.events
            report["captured_event_count"] = len(session.events)
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                if failure is None:
                    failure = RuntimeError("; ".join(cleanup_errors))
            report["success"] = failure is None
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        raise RuntimeError(f"Audio/control performance QA failed; evidence: {output}: {failure}") from failure
    return {"path": str(output), "success": True,
            "captured_event_count": len(session.events),
            "host_inputs_before": initial["inputs_tx"],
            "host_inputs_after": report["after"]["inputs_tx"],
            "phases": [{key: phase[key] for key in
                        ("control", "render_max_us", "input_interval_max_us", "audio_delta")}
                       for phase in report["phases"]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?",
                        default=ROOT / "artifacts/audio-performance-qa-0.10.2.json")
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--max-render-us", type=int)
    parser.add_argument("--max-input-us", type=int)
    args = parser.parse_args()
    if args.timeout <= 0 or any(value is not None and value <= 0
                                for value in (args.max_render_us, args.max_input_us)):
        parser.error("Timeout and optional timing limits must be positive")
    try:
        result = verify(args.output, args.port, args.timeout,
                        args.max_render_us, args.max_input_us)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
