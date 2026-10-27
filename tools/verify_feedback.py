#!/usr/bin/env python3
"""Verify local feedback, settings gestures and persistence on firmware 0.11.0.

SOUND_TEST deliberately exercises the hardware audio queue in input capture
mode. Worker counters prove submission/retirement, not acoustic output. All
native input is captured locally; no native HID handle or desktop action is used.
Two controlled restarts verify changed preferences and then their restoration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from tab5_transport import open_serial
from verify_neon import Session, require


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = "tab5-micro-0.11.0"
GEAR = (888, 49)
MUTE = (164, 324)
SLIDER_LEFT, SLIDER_RIGHT = 320, 1120
VOLUME_Y, BRIGHTNESS_Y = 300, 500
PERSIST_WAIT = .9  # Firmware debounce is 600ms, after touch release.
TEST_VOLUME = 22  # A modest test level; endpoint tests never play at volume100.


class FeedbackSession(Session):
    def screenshot(self, path: Path) -> dict:
        capture = super().screenshot(path)
        capture["synthetic_preview"] = False
        capture["source"] = "actual_device_settings_framebuffer"
        return capture

    def sound_test(self, cue: str) -> str:
        require(self.status().get("input_capture") is True,
                "Refusing SOUND_TEST outside input capture")
        # Valid tests ACK even when mute/zero volume suppresses playback.
        self.command(f"SOUND_TEST {cue}", "SOUND_TEST_OK")
        return "SOUND_TEST_OK"

    def tap(self, x: int, y: int) -> None:
        self.touch(x, y, True)
        self.touch(x, y, False)
        time.sleep(.1)


def preferences(current: dict) -> dict:
    return {"enabled": current["audio"]["enabled"],
            "volume": current["audio"]["volume"],
            "brightness_percent": current["brightness_percent"]}


def check_preferences(current: dict, expected: dict) -> None:
    actual = preferences(current)
    require(actual == expected, f"Settings mismatch: expected {expected}, got {actual}")
    percent = expected["brightness_percent"]
    require(current["brightness"] == (percent * 255 + 50) // 100,
            f"Display brightness does not match {percent}%: {current['brightness']}")


def set_preferences(session: FeedbackSession, value: dict) -> None:
    # CDC setters preserve enabled independently from volume, unlike the UI.
    session.command(f"VOLUME {value['volume']}", f"VOLUME {value['volume']}")
    sound = "SOUND ON" if value["enabled"] else "SOUND OFF"
    session.command(sound, sound)
    brightness = f"BRIGHTNESS {value['brightness_percent']}"
    session.command(brightness, brightness)
    check_preferences(session.status(), value)


def wait_saved(session: FeedbackSession) -> dict:
    time.sleep(PERSIST_WAIT)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        current = session.status()
        if current.get("settings_pending") is False:
            return current
        time.sleep(.15)
    raise TimeoutError("Local preferences are still pending after the persistence debounce")


def check_safety(session: FeedbackSession, current: dict, baseline: dict,
                 capture: bool = True) -> None:
    require(current["firmware"] == FIRMWARE, "Unexpected firmware after reconnect")
    require(current["inputs_tx"] == baseline["inputs_tx"],
            "Native host input counter changed")
    require(current["input_capture"] is capture, "Unexpected input capture state")
    require(current["captured_inputs"] - baseline["captured_inputs"] == len(session.events),
            "Captured input count does not match the collected event list")


def wait_audio(session: FeedbackSession, predicate, timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = session.status()
        if predicate(last["audio"]):
            return last
        time.sleep(.1)
    raise TimeoutError(f"Audio worker did not reach expected state: {last and last['audio']}")


def begin(session: FeedbackSession, baseline: dict) -> None:
    session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
    current = session.status()
    check_safety(session, current, baseline)
    require(current["effects_frozen"] is True, "Capture did not freeze decorative effects")


def end(session: FeedbackSession, baseline: dict) -> dict:
    require(session.status().get("input_capture") is True,
            "Capture ended unexpectedly before controlled cleanup")
    session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
    current = session.status()
    check_safety(session, current, baseline, capture=False)
    require(not current["lighting_preview"], "Unexpected lighting preview after END")
    return current


def reconnect(port: str | None, timeout: float, deadline_seconds: float = 22,
              fresh_boot: bool = True):
    deadline = time.monotonic() + deadline_seconds
    last_error = None
    while time.monotonic() < deadline:
        connection = None
        try:
            # Resolve the exact VID/PID/serial anew; a macOS CDC path may change.
            connection = open_serial(port, min(timeout, 3))
            session = FeedbackSession(connection, timeout)
            current = session.status()
            require(current["firmware"] == FIRMWARE, "Wrong firmware after restart")
            if fresh_boot:
                require(not current["input_capture"], "Capture survived restart")
                require(not current["settings_open"], "Settings page survived restart")
                require(current["inputs_tx"] == 0 and current["captured_inputs"] == 0,
                        "Restarted device already emitted or captured native input")
            return connection, session, current
        except (OSError, RuntimeError, TimeoutError) as error:
            last_error = error
            if connection is not None:
                connection.close()
            time.sleep(.3)
    raise TimeoutError(f"Exact Tab5 did not reconnect after restart: {last_error}")


def restart(session: FeedbackSession, baseline: dict, port: str | None, timeout: float):
    current = session.status()
    check_safety(session, current, baseline, capture=False)
    # No acknowledgement is expected for RESTART. Release the serial lock
    # before waiting and resolving the exact application device again.
    try:
        session.send("RESTART")
    except (OSError, TimeoutError):
        # ESP.restart may detach CDC while pyserial flushes the already-sent
        # command. A fresh STATUS below is the confirmation, rather than ACK.
        pass
    finally:
        session.connection.close()
    time.sleep(2)
    # --port selects the initial connection only. Identity, rather than an old
    # macOS path, is the authority for a device that has just re-enumerated.
    return reconnect(None, timeout)


def verify(output: Path, port: str | None = None, timeout: float = 10,
           max_enqueue_us: int | None = None, max_start_us: int | None = None) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {"firmware": FIRMWARE, "success": False,
                    "sound_test_volume": TEST_VOLUME,
                    "audio_evidence_limit": "Queue/worker retirement counters do not prove acoustic output",
                    "checks": {}, "boot_sessions": [], "captures": []}
    connection = None
    session = None
    baseline = None
    initial = None
    initial_page = False
    settings_mutated = False
    error: BaseException | None = None
    try:
        connection = open_serial(port, timeout)
        session = FeedbackSession(connection, timeout)
        baseline = session.status()
        require(baseline["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not baseline["input_capture"] and not baseline["lighting_preview"],
                "Another capture/preview session is active")
        initial = preferences(baseline)
        initial_page = baseline["settings_open"]
        report["initial_preferences"] = initial
        report["before"] = baseline
        begin(session, baseline)

        settings_mutated = True
        session.command("SETTINGS CLOSE", "SETTINGS CLOSE")
        # The header gesture owns the complete drag even after opening settings.
        session.touch(*GEAR, True)
        session.touch(1140, 617, True)
        session.touch(1140, 617, False)
        current = session.status()
        require(current["settings_open"] is True, "Gear did not open settings")
        require(current["theme"] == baseline["theme"], "Gear drag changed theme")
        require(not session.events, "Gear drag leaked native input")
        report["checks"]["gear_drag_capture"] = True

        # Endpoint tests also exercise UI semantics: zero mutes, nonzero unmutes.
        for x, volume, enabled in ((SLIDER_LEFT, 0, False), (SLIDER_RIGHT, 100, True)):
            session.tap(x, VOLUME_Y)
            current = session.status()
            require(current["audio"]["volume"] == volume
                    and current["audio"]["enabled"] is enabled,
                    f"Volume slider endpoint failed: {current['audio']}")
        for x, percent in ((SLIDER_LEFT, 10), (SLIDER_RIGHT, 100)):
            session.tap(x, BRIGHTNESS_Y)
            current = session.status()
            require(current["brightness_percent"] == percent
                    and current["brightness"] == (percent * 255 + 50) // 100,
                    f"Brightness slider endpoint failed: {current['brightness_percent']}")
        report["checks"]["slider_endpoints"] = {"volume": [0, 100], "brightness_percent": [10, 100]}

        # A volume gesture cannot turn into the adjacent mute button, header
        # theme button, or a native command as its position crosses them.
        session.touch(720, VOLUME_Y, True)
        session.touch(*MUTE, True)
        session.touch(1015, 49, True)
        session.touch(103, 617, True)
        session.touch(103, 617, False)
        current = session.status()
        require(current["settings_open"] is True and current["theme"] == baseline["theme"],
                "Slider drag escaped its settings control")
        require(current["audio"]["volume"] == 0 and current["audio"]["enabled"] is False,
                "Volume drag did not clamp to zero")
        require(not session.events, "Slider drag leaked native input")
        # Unmuting from zero restores the documented default volume.
        session.tap(*MUTE)
        current = session.status()
        require(current["audio"]["enabled"] is True and current["audio"]["volume"] == 45,
                "Unmuting at zero did not restore volume45")
        session.tap(*MUTE)
        current = session.status()
        require(current["audio"]["enabled"] is False and current["audio"]["volume"] == 45,
                "Mute did not preserve the previous nonzero volume")
        check_safety(session, current, baseline)
        report["checks"]["slider_drag_capture_and_mute"] = True
        report["checks"]["settings_native_events"] = 0

        # Genuine short PCM clips are submitted one at a time. The queue has
        # intentionally bounded capacity; do not turn this into a flood test.
        session.command(f"VOLUME {TEST_VOLUME}", f"VOLUME {TEST_VOLUME}")
        session.command("SOUND ON", "SOUND ON")
        time.sleep(.2)
        audio_before = session.status()["audio"]
        require(audio_before["available"] is True, "Audio device is unavailable")
        played = []
        for cue in ("PRESS", "CW", "CC", "DIRECTION", "NEUTRAL"):
            previous = session.status()["audio"]
            ack = session.sound_test(cue)
            current = wait_audio(session, lambda audio:
                                 audio["played"] >= previous["played"] + 1
                                 and audio["released"] >= previous["released"] + 1)
            require(current["audio"]["dropped"] == previous["dropped"],
                    f"The {cue} test cue was dropped")
            played.append({"cue": cue, "ack": ack, "audio": current["audio"]})
            time.sleep(.12)
        audio_after = session.status()["audio"]
        require(audio_after["played"] - audio_before["played"] == 5,
                "Unexpected number of submitted test clips played")
        if max_enqueue_us is not None:
            require(audio_after["enqueue_max_us"] <= max_enqueue_us,
                    "Audio enqueue exceeded the requested timing limit")
        if max_start_us is not None:
            require(audio_after["start_max_us"] <= max_start_us,
                    "Audio worker start exceeded the requested timing limit")
        report["checks"]["hardware_worker_cues"] = {
            "before": audio_before, "after": audio_after, "cues": played,
            "meaning": "Five submissions started and their buffers retired; not an acoustic measurement",
        }

        for name, enabled, volume in (("volume_zero", True, 0), ("master_muted", False, TEST_VOLUME)):
            session.command(f"VOLUME {volume}", f"VOLUME {volume}")
            sound = "SOUND ON" if enabled else "SOUND OFF"
            session.command(sound, sound)
            time.sleep(.2)  # Previous cue has already retired; allow idle work to settle.
            muted_before = session.status()["audio"]
            ack = session.sound_test("PRESS")
            time.sleep(.3)
            muted_after = session.status()["audio"]
            require(muted_after["played"] == muted_before["played"],
                    f"Muted test started playback: {name}")
            require(muted_after["queued"] == muted_before["queued"],
                    f"Muted test entered the hardware queue: {name}")
            require(muted_after["suppressed"] > muted_before["suppressed"],
                    f"Muted test was not accounted as suppressed: {name}")
            report["checks"][name] = {"ack": ack, "before": muted_before, "after": muted_after}

        # Default capture suppression is distinct from explicit SOUND_TEST.
        session.command(f"VOLUME {TEST_VOLUME}", f"VOLUME {TEST_VOLUME}")
        session.command("SOUND ON", "SOUND ON")
        session.command("SETTINGS CLOSE", "SETTINGS CLOSE")
        time.sleep(.2)
        captured_before = session.status()["audio"]
        session.tap(125, 151)
        time.sleep(.2)
        current = session.status()
        expected_events = [{"m": "v.oai.hid", "p": {"k": "AG00", "act": action}}
                           for action in (1, 0)]
        require(session.events == expected_events, f"Unexpected captured native events: {session.events}")
        require(current["audio"]["played"] == captured_before["played"],
                "Normal captured native key unexpectedly played audio")
        require(current["audio"]["queued"] == captured_before["queued"],
                "Normal captured native key unexpectedly entered the hardware queue")
        require(current["audio"]["suppressed"] > captured_before["suppressed"],
                "Normal captured cue was not accounted as suppressed")
        check_safety(session, current, baseline)
        report["checks"]["normal_capture_audio_suppression"] = True

        probe = {"enabled": not initial["enabled"],
                 "volume": 37 if initial["volume"] != 37 else 53,
                 "brightness_percent": 61 if initial["brightness_percent"] != 61 else 73}
        set_preferences(session, probe)
        session.command("SETTINGS CLOSE", "SETTINGS CLOSE")
        report["checks"]["probe_saved_before_restart"] = wait_saved(session)
        first_end = end(session, baseline)
        report["boot_sessions"].append({"purpose": "UI and audio QA", "before": baseline,
                                         "after": first_end, "captured_events": session.events})
        connection, session, baseline = restart(session, baseline, port, timeout)
        check_preferences(baseline, probe)
        require(baseline["theme"] == report["before"]["theme"], "Theme changed on restart")
        report["checks"]["changed_preferences_survive_restart"] = {"expected": probe, "actual": baseline}

        begin(session, baseline)
        set_preferences(session, initial)
        session.command("SETTINGS OPEN", "SETTINGS OPEN")
        time.sleep(.5)
        session.command("FX FREEZE", "FX FREEZE")  # Renew capture before SCREEN.
        capture = session.screenshot(output.parent /
                                     f"tab5-settings-restored-{baseline['theme']}-0.11.0.png")
        report["captures"].append(capture)
        check_preferences(session.status(), initial)
        report["checks"]["restoration_saved_before_restart"] = wait_saved(session)
        second_end = end(session, baseline)
        report["boot_sessions"].append({"purpose": "Restore original preferences", "before": baseline,
                                         "after": second_end, "captured_events": session.events})
        connection, session, baseline = restart(session, baseline, port, timeout)
        check_preferences(baseline, initial)
        report["checks"]["restored_preferences_survive_restart"] = baseline
        require(not baseline["input_capture"] and not baseline["lighting_preview"],
                "Diagnostic state survived restart")
        page = "SETTINGS OPEN" if initial_page else "SETTINGS CLOSE"
        session.command(page, page)
        report["after"] = session.status()
        check_preferences(report["after"], initial)
        require(report["after"]["inputs_tx"] == 0 and not session.events,
                "Final restored device emitted input")
        report["success"] = True
    except BaseException as failure:
        error = failure
        report["error"] = f"{type(failure).__name__}: {failure}"
    finally:
        cleanup_errors = []
        if not report["success"] and session is not None and initial is not None:
            stream_ready = True
            if session.frame_remaining is not None:
                try:
                    session.recover_frame()
                except Exception as failure:
                    stream_ready = False
                    cleanup_errors.append(f"Framebuffer recovery: {failure}")
            transport_ready = False
            if stream_ready and connection is not None and connection.is_open:
                try:
                    session.status()
                    transport_ready = True
                except Exception as failure:
                    cleanup_errors.append(f"Existing transport: {failure}")
                    connection.close()
            if stream_ready and not transport_ready:
                try:
                    connection, session, baseline = reconnect(
                        None, timeout, deadline_seconds=8, fresh_boot=False)
                except Exception as failure:
                    stream_ready = False
                    cleanup_errors.append(f"Cleanup reconnect: {failure}")
            if stream_ready:
                cleanup_capture = False
                try:
                    cleanup_capture = session.status().get("input_capture") is True
                    if cleanup_capture:
                        # Lift a failed drag before restoring preferences, or
                        # the still-held slider could overwrite them again.
                        session.touch(0, 0, False)
                except Exception as failure:
                    cleanup_errors.append(f"Diagnostic release: {failure}")
                if settings_mutated:
                    commands = [
                        f"VOLUME {initial['volume']}",
                        "SOUND ON" if initial["enabled"] else "SOUND OFF",
                        f"BRIGHTNESS {initial['brightness_percent']}",
                        "SETTINGS OPEN" if initial_page else "SETTINGS CLOSE",
                    ]
                    for command in commands:
                        try:
                            session.command(command, command)
                        except Exception as failure:
                            cleanup_errors.append(f"{command}: {failure}")
                    try:
                        wait_saved(session)
                        check_preferences(session.status(), initial)
                    except Exception as failure:
                        cleanup_errors.append(f"Preference restoration: {failure}")
                try:
                    if session.status().get("input_capture") is True:
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                    report["cleanup_status"] = session.status()
                except Exception as failure:
                    cleanup_errors.append(f"Capture cleanup: {failure}")
            else:
                cleanup_errors.append("Capture watchdog must clear diagnostics; serial stream is unsynchronized")
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
        if connection is not None:
            connection.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if error is not None:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise error
        raise RuntimeError(f"Feedback QA failed; evidence: {output}: {error}") from error
    return {"path": str(output), "success": True, "controlled_restarts": 2,
            "settings_native_events": 0, "normal_capture_test_events": 2,
            "sound_tests_played": 5, "host_inputs_after_final_restart": report["after"]["inputs_tx"],
            "captures": [capture["path"] for capture in report["captures"]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?",
                        default=ROOT / "artifacts/feedback-qa-0.11.0.json")
    parser.add_argument("--port", help="Optional initial port; exact device identity is rediscovered after reboot")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--max-enqueue-us", type=int)
    parser.add_argument("--max-start-us", type=int)
    args = parser.parse_args()
    if args.timeout <= 0 or any(value is not None and value <= 0
                                for value in (args.max_enqueue_us, args.max_start_us)):
        parser.error("Timeout and optional timing limits must be positive")
    try:
        result = verify(args.output, args.port, args.timeout,
                        args.max_enqueue_us, args.max_start_us)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
