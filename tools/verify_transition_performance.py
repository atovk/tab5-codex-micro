#!/usr/bin/env python3
"""Measure complete UI transitions without framebuffer traffic or desktop actions.

Each phase resets device metrics after a quiet preflight, performs one local
transition, waits, and reads its first STATUS. Render calls finish before the
firmware dispatches STATUS, so that response includes the completed redraw.
Input intervals are firmware service-loop timings during captured diagnostics;
they do not prove physical touch-to-pixel latency or desktop action execution.
No SCREEN, live title update, HOST_CONFIG, restart, or native HID access is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

from tab5_transport import open_serial
from verify_neon import require
from verify_title_scroll import TitleSession as Session, encode, host_fixture, preview_fixtures


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.11.1"
METRICS = ("render_last_us", "render_max_us", "render_samples",
           "input_interval_last_us", "input_interval_max_us", "input_interval_samples")
SHORT_TITLES = tuple(f"Preview {index + 1}" for index in range(6))
LONG_TITLES = tuple(
    (f"Synthetic slot {index + 1}: validate complete title replacement, two-line layout, "
     "and responsive controls while a new long title is rasterized.")
    if index % 2 == 0 else
    (f"合成标题{index + 1}：春夏秋冬天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
     "测试完整标题首次绘制与双行排版以及界面响应")
    for index in range(6))
LONG_TRACKED_TITLE = (
    "Synthetic tracked chat: render a new long title while preserving responsive "
    "controls and accurate state feedback through the complete transition."
)


def firmware_name(version: str) -> str:
    if version.startswith("tab5-micro-"):
        version = version.removeprefix("tab5-micro-")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Firmware must be an exact version such as 0.11.1")
    return "tab5-micro-" + version


def metadata(status: dict) -> dict:
    """Keep timings and local state only: STATUS can contain real chat titles."""
    fields = ("firmware", "uptime_ms", "theme", "language", "playing", "settings_open",
              "settings_pending", "input_capture", "lighting_preview", "effects_frozen",
              "inputs_tx", "captured_inputs", "game", "game_state", *METRICS)
    result = {key: status[key] for key in fields if key in status}
    result["ui_profile"] = {key: value for key, value in status.get("ui_profile", {}).items()
                            if type(value) in (int, float, bool)}
    result["title_ready"] = [entry.get("ready") for entry in status.get("title_scroll", [])]
    return result


def safety(status: dict, baseline: dict, session: Session, capture: bool = True) -> None:
    require(status["firmware"] == baseline["firmware"], "Firmware changed during QA")
    require(status["inputs_tx"] == baseline["inputs_tx"], "Native host input counter changed")
    require(status["captured_inputs"] == baseline["captured_inputs"] and not session.events,
            "Unexpected captured native input during transition-only QA")
    require(status["input_capture"] is capture, "Input capture state is unexpected")


def quiet(seconds: float) -> None:
    """Bound each wait; no serial polling may contaminate the measurement."""
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(.5, remaining))


def assert_state(status: dict, expected: dict) -> None:
    for key, value in expected.items():
        # 0.11.0 does not export its page flag. Its PLAY/MICRO ACK confirms the
        # page request, and a completed render is required separately below.
        if key == "playing" and key not in status:
            continue
        require(status.get(key) == value, f"Transition did not apply {key}={value!r}")


def run_phase(session: Session, baseline: dict, name: str, command: str,
              expected: dict, settle_seconds: float, acknowledgement: str | None = None,
              followups: tuple[tuple[str, str], ...] = ()) -> dict:
    session.command("FX FREEZE", "FX FREEZE")  # Also renew the capture watchdog.
    before = session.status()
    safety(before, baseline, session)
    require(before.get("effects_frozen") is True, "Capture effects must remain frozen")
    # Complete the prior STATUS transfer and its following loop interval before
    # reset; otherwise that diagnostic write can dominate the next phase.
    quiet(.25)
    session.command("PERF_RESET", "OK")
    began = time.monotonic()
    session.command(command, acknowledgement or command)
    for value, expected_ack in followups:
        session.command(value, expected_ack)
    ack_seconds = time.monotonic() - began
    quiet(settle_seconds)
    # The first STATUS copies counters before transmitting its large JSON body.
    # No framebuffer or STATUS polling occurs between reset and this snapshot.
    after = session.status()
    safety(after, baseline, session)
    assert_state(after, expected)
    require(all(type(after.get(key)) is int and after[key] >= 0 for key in METRICS),
            f"{name}: incomplete or invalid device timing counters")
    require(after["render_samples"] > 0, f"{name}: no completed redraw after PERF_RESET")
    require(after["input_interval_samples"] > 0, f"{name}: no input-service samples")
    if name.startswith("cold_"):
        panels = after.get("title_scroll", [])
        indices = range(6) if name == "cold_slot_titles" else (6,)
        require(len(panels) == 7 and all(panels[index].get("ready") for index in indices),
                f"{name}: title rasterization did not complete")
    return {"name": name,
            "commands": [value.split(" ", 1)[0] for value in (command, *(v for v, _ in followups))],
            "expected_state": expected, "settle_seconds": settle_seconds,
            "command_ack_seconds": round(ack_seconds, 6),
            "measurement_window_seconds": round(time.monotonic() - began, 6),
            **{key: after[key] for key in METRICS},
            "ui_profile": metadata(after)["ui_profile"],
            "title_ready": metadata(after)["title_ready"],
            "completion": "First STATUS after quiet settle; synchronous redraw has returned"}


def wait_saved(session: Session, baseline: dict, capture: bool, timeout: float) -> dict:
    # Both preference persistence and UI rendering are asynchronous to ACKs.
    quiet(.9)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = session.status()
        safety(current, baseline, session, capture)
        if current.get("settings_pending") is False:
            return current
        quiet(.15)
    raise TimeoutError("Restored preferences are still pending")


def verify(output: Path, port: str | None = None, timeout: float = 10,
           firmware: str = VERSION, settle_seconds: float = 2,
           max_input_us: int | None = None) -> dict:
    expected_firmware = firmware_name(firmware)
    require(timeout > 0 and 1.5 <= settle_seconds <= 10,
            "Timeout must be positive and settle-seconds must be 1.5..10")
    require(max_input_us is None or max_input_us > 0, "Input timing limit must be positive")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"firmware": expected_firmware, "success": False, "phases": [],
              "synthetic_preview_only": True, "screenshots": False,
              "max_input_limit_us": max_input_us,
              "scope": "Local UI transitions in owned capture; firmware input-service intervals "
                       "and completed render timing, not physical touch or desktop action proof",
              "cold_title_scope": "New title-layout/raster cache entries; font storage may already be warm",
              "measurement": "Per-phase PERF_RESET; short command ACKs then quiet settle; first STATUS only"}
    failure: BaseException | None = None
    with open_serial(port, timeout) as connection:
        session = Session(connection, timeout)
        baseline = session.status()
        require(baseline["firmware"] == expected_firmware, f"Expected {expected_firmware}")
        require(not baseline["input_capture"] and not baseline["lighting_preview"],
                "Another diagnostic capture/preview is active")
        require(baseline["theme"] in ("light", "dark") and baseline["language"] in ("en", "zh"),
                "Unrecognized starting preferences")
        require(type(baseline.get("settings_open")) is bool, "Missing initial settings page state")
        report["before"] = metadata(baseline)
        began = False
        page_changed = False
        try:
            if baseline.get("playing") is True:
                # INPUT_TEST_BEGIN rejects PLAY. MICRO is a local header action,
                # and its page is restored even if capture fails to begin.
                page_changed = True
                session.command("MICRO", "MICRO")
            began = True  # An ACK timeout can still leave capture enabled.
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            current = session.status()
            safety(current, baseline, session)
            if "playing" not in baseline:
                # In 0.11.0 successful BEGIN is a firmware-enforced proof that
                # the original page was MICRO (possibly with settings open).
                baseline["playing"] = False
                report["legacy_page_evidence"] = "INPUT_TEST_BEGIN accepted only outside PLAY"
            assert_state(current, {"playing": False, "effects_frozen": True})
            page_changed = True
            for command in ("MICRO", "SETTINGS CLOSE", "THEME DARK", "LANGUAGE ZH"):
                session.command(command, command)
            session.command("PREVIEW_LIGHTS " + encode(preview_fixtures(SHORT_TITLES)),
                            "PREVIEW_LIGHTS_OK")
            session.command("PREVIEW_HOST " + encode(host_fixture("Synthetic tracked preview")),
                            "PREVIEW_HOST_OK")
            # Finish initial/preexisting full redraws before any phase reset.
            quiet(settle_seconds)
            initial = session.status()
            safety(initial, baseline, session)
            assert_state(initial, {"theme": "dark", "language": "zh", "playing": False,
                                   "settings_open": False, "lighting_preview": True})
            phases = [
                ("theme_light", "THEME LIGHT", {"theme": "light"}, None),
                ("theme_dark", "THEME DARK", {"theme": "dark"}, None),
                ("language_en", "LANGUAGE EN", {"language": "en"}, None),
                ("language_zh", "LANGUAGE ZH", {"language": "zh"}, None),
                ("settings_open", "SETTINGS OPEN", {"settings_open": True}, None),
                ("settings_close", "SETTINGS CLOSE", {"settings_open": False}, None),
                ("play", "PLAY", {"playing": True, "settings_open": False}, None),
                ("micro", "MICRO", {"playing": False, "settings_open": False}, None),
                ("cold_slot_titles", "PREVIEW_LIGHTS " + encode(preview_fixtures(LONG_TITLES)),
                 {"lighting_preview": True}, "PREVIEW_LIGHTS_OK"),
                ("cold_tracked_title", "PREVIEW_HOST " + encode(host_fixture(LONG_TRACKED_TITLE)),
                 {"lighting_preview": True}, "PREVIEW_HOST_OK"),
            ]
            for name, command, expected, acknowledgement in phases:
                # PREVIEW_LIGHTS seeds from the live model, so immediately
                # reapply the synthetic tracked title too. This phase measures
                # the complete pair, never a live title/configuration write.
                followups = (("PREVIEW_HOST " + encode(host_fixture("Synthetic tracked preview")),
                              "PREVIEW_HOST_OK"),) if name == "cold_slot_titles" else ()
                phase = run_phase(session, baseline, name, command, expected,
                                  settle_seconds, acknowledgement, followups)
                phase["within_input_limit"] = (None if max_input_us is None else
                                                phase["input_interval_max_us"] <= max_input_us)
                report["phases"].append(phase)
            slow = [phase["name"] for phase in report["phases"]
                    if phase["within_input_limit"] is False]
            require(not slow, "Input service interval exceeded requested limit: " + ", ".join(slow))
        except BaseException as error:
            failure = error
            report["error"] = f"{type(error).__name__}: {error}"
        finally:
            cleanup_errors = []
            stream_ready = True
            try:
                session.recover_frame()
            except Exception as error:
                cleanup_errors.append(f"Framebuffer recovery: {error}")
                stream_ready = False
            if stream_ready:
                # Only this serial-lock owner may clean up its attempted BEGIN.
                # If BEGIN was rejected, do not END a non-capture real gesture.
                capture = False
                try:
                    capture = began and session.status().get("input_capture") is True
                except Exception as error:
                    cleanup_errors.append(f"Read capture state: {error}")
                if capture or page_changed:
                    commands = ["MICRO", "SETTINGS CLOSE",
                                f"THEME {baseline['theme'].upper()}",
                                f"LANGUAGE {baseline['language'].upper()}"]
                    if capture:
                        commands.insert(0, "PREVIEW_LIGHTS_CLEAR")
                    if baseline.get("playing"):
                        commands.append("PLAY")
                    elif baseline["settings_open"]:
                        commands.append("SETTINGS OPEN")
                    for command in commands:
                        try:
                            session.command(command, "PREVIEW_LIGHTS_CLEARED"
                                            if command == "PREVIEW_LIGHTS_CLEAR" else command)
                        except Exception as error:
                            cleanup_errors.append(f"{command}: {error}")
                if capture:
                    try:
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                    except Exception as error:
                        cleanup_errors.append(f"INPUT_TEST_END: {error}")
                try:
                    after = wait_saved(session, baseline, False, timeout)
                    report["after"] = metadata(after)
                    assert_state(after, {"theme": baseline["theme"], "language": baseline["language"],
                                         "settings_open": baseline["settings_open"],
                                         "playing": baseline.get("playing", False),
                                         "lighting_preview": False,
                                         "effects_frozen": baseline["effects_frozen"]})
                except Exception as error:
                    cleanup_errors.append(f"Final state/preferences: {error}")
            else:
                cleanup_errors.append("Text cleanup skipped because binary recovery failed; "
                                      "the firmware capture watchdog must perform cleanup")
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
        raise RuntimeError(f"Transition QA failed; evidence: {output}: {failure}") from failure
    return {"path": str(output), "success": True,
            "host_inputs_before": baseline["inputs_tx"],
            "host_inputs_after": report["after"]["inputs_tx"],
            "captured_event_count": report["captured_event_count"],
            "phases": [{key: phase[key] for key in
                        ("name", "render_max_us", "input_interval_max_us", "within_input_limit")}
                       for phase in report["phases"]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--port")
    parser.add_argument("--firmware", default=VERSION, help="Exact expected version (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--settle-seconds", type=float, default=2,
                        help="Quiet wait after each ACK, 1.5..10 (default: %(default)s)")
    parser.add_argument("--max-input-us", type=int,
                        help="Optional failure threshold; omitted for baseline-only reporting")
    args = parser.parse_args()
    try:
        firmware = firmware_name(args.firmware)
        output = args.output or ROOT / f"artifacts/transition-performance-{firmware.removeprefix('tab5-micro-')}.json"
        result = verify(output, args.port, args.timeout, firmware,
                        args.settle_seconds, args.max_input_us)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
