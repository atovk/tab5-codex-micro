#!/usr/bin/env python3
"""Exercise the real touch UI and native event serializer without host actions.

Firmware INPUT_TEST capture mode suppresses HID input notifications and sends
their exact JSON over the locked CDC interface. The test always exits that mode.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time

from tab5_transport import open_serial
from verify_device import read_line


def verify(output: Path, firmware: str = "0.11.1") -> dict:
    events: list[dict] = []
    checks: dict = {}
    with open_serial(timeout=12) as connection:
        def command(text: str, expected: str, timeout: float = 5) -> None:
            connection.write((text + "\n").encode())
            connection.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = read_line(connection, deadline).decode("utf-8", errors="replace")
                if line.startswith("INPUT_EVENT "):
                    events.append(json.loads(line.removeprefix("INPUT_EVENT ")))
                if line == expected:
                    return
                if "ERROR" in line:
                    raise RuntimeError(line)
            raise TimeoutError(f"Missing acknowledgement: {expected}")

        def status() -> dict:
            connection.write(b"STATUS\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                line = read_line(connection, deadline)
                if line.startswith(b"INPUT_EVENT "):
                    events.append(json.loads(line.removeprefix(b"INPUT_EVENT ")))
                if line.startswith(b"{"):
                    value = json.loads(line)
                    if "firmware" in value:
                        return value
            raise TimeoutError("Missing device status")

        def touch(x: int, y: int, pressed: bool) -> None:
            command(f"INPUT_TOUCH {x} {y} {int(pressed)}", "INPUT_TOUCH_OK")

        def hid(key: str, action: int) -> dict:
            return {"m": "v.oai.hid", "p": {"k": key, "act": action}}

        before = status()
        expected = firmware if firmware.startswith("tab5-micro-") else "tab5-micro-" + firmware
        assert before["firmware"] == expected, before["firmware"]
        assert before["usb_mounted"] and before["host_seen"], before
        command("MICRO", "MICRO")
        command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
        try:
            # Theme selection owns the complete gesture, including a drag
            # across a command key. It must never emit a desktop input.
            start = len(events)
            initial_theme = before["theme"]
            touch(1015,49,True)
            touch(1140,617,True)
            touch(1140,617,False)
            assert status()["theme"] != initial_theme
            touch(1015,49,True)
            touch(1015,49,False)
            assert status()["theme"] == initial_theme
            assert len(events) == start, events[start:]
            checks["theme_toggle_and_drag_capture"] = True

            start = len(events)
            expected_theme = initial_theme
            for y in (5,92):
                touch(1015,y,True)
                touch(125,151,True)
                touch(125,151,False)
                expected_theme = "dark" if expected_theme == "light" else "light"
                assert status()["theme"] == expected_theme, (y,expected_theme)
            assert status()["theme"] == initial_theme
            for y in (4,93):
                touch(1015,y,True); touch(1015,y,False)
                assert status()["theme"] == initial_theme, y
            assert status()["theme"] == initial_theme
            assert events[start:] == [], events[start:]
            checks["header_expanded_target_and_gaps"] = True

            for index in range(6):
                key = f"AG{index:02d}"
                x = 125+206*index
                for y in (106,195):
                    start = len(events)
                    touch(x,y,True); touch(x,y,False)
                    assert events[start:] == [hid(key,1),hid(key,0)], (key,y,events[start:])
                for y in (105,196):
                    start = len(events)
                    touch(x,y,True); touch(x,151,True); touch(x,151,False)
                    assert not events[start:], (key,y,events[start:])
            checks["agent_expanded_targets_and_gaps"] = True

            for clockwise in (True, False):
                start = len(events)
                for step in range(73):
                    angle = (1 if clockwise else -1) * math.tau * step / 72
                    touch(round(244+96*math.cos(angle)), round(352+96*math.sin(angle)), True)
                touch(340,352,False)
                key = "ENC_CW" if clockwise else "ENC_CC"
                turns = events[start:]
                assert len(turns) == 24, (key,len(turns),turns)
                assert all(event == hid(key,2) for event in turns), turns
                checks[key] = len(turns)

            start = len(events)
            touch(244,352,True)
            time.sleep(.65)
            touch(246,353,True)
            touch(244,352,False)
            assert events[start:] == [hid("ENC",1),hid("ENC",0)], events[start:]
            checks["encoder_hold_ms"] = 650

            targets = [(28+206*i+97,151,f"AG{i:02d}") for i in range(6)]
            targets += [(103,617,"ACT06"),(265,617,"ACT07"),(427,617,"ACT08"),
                        (589,617,"ACT09"),(761,617,"ACT10"),(931,617,"ACT11"),
                        (1140,617,"ACT12")]
            for x,y,key in targets:
                start = len(events)
                touch(x,y,True); touch(x,y,True); touch(x,y,False)
                assert events[start:] == [hid(key,1),hid(key,0)], (key,events[start:])
            checks["keys_with_balanced_edges"] = len(targets)

            # A held key owns the gesture even after crossing another key.
            start = len(events)
            touch(125,151,True); touch(1140,617,True); touch(1140,617,False)
            assert events[start:] == [hid("AG00",1),hid("AG00",0)], events[start:]
            checks["gesture_capture"] = True

            cardinal = [(894,352,0),(802,444,.25),(710,352,.5),(802,260,.75)]
            for x,y,angle in cardinal:
                start = len(events)
                touch(802,352,True)
                touch(x,y,True)
                # Holding a direction must not synthesize repeated actions.
                time.sleep(.06)
                touch(x,y,True)
                touch(x,y,False)
                direction = events[start:]
                assert len(direction) == 3, direction
                assert all(event["m"] == "v.oai.rad" for event in direction), direction
                assert direction[0]["p"]["d"] == 0, direction
                assert abs(direction[1]["p"]["a"]-angle) < .0001, direction
                assert abs(direction[1]["p"]["d"]-1) < .0001, direction
                assert direction[-1]["p"]["d"] == 0, direction
            checks["joystick_cardinal_and_neutral"] = True

            # Complete the preceding stick's neutral render before measuring
            # only dial motion; otherwise the first frame contains both controls.
            time.sleep(.2)
            command("PERF_RESET", "OK")
            start = len(events)
            for step in range(25):
                angle = math.tau*step/24
                touch(round(244+96*math.cos(angle)),round(352+96*math.sin(angle)),True)
            touch(340,352,False)
            turns = events[start:]
            assert len(turns) == 24 and all(event == hid("ENC_CW",2) for event in turns), turns
            checks["ENC_CW_sparse_samples"] = len(turns)
            time.sleep(.2)
            dial_metrics = status()
            checks["dial_render_max_us"] = dial_metrics["render_max_us"]
            checks["dial_input_interval_max_us"] = dial_metrics["input_interval_max_us"]
            checks["dial_profile"] = dial_metrics.get("ui_profile",{})

            command("PERF_RESET", "OK")
            start = len(events)
            touch(802,352,True)
            for step in range(25):
                angle = math.tau*step/24
                touch(round(802+80*math.cos(angle)),round(352+80*math.sin(angle)),True)
            touch(882,352,False)
            stick_events = events[start:]
            assert stick_events and all(event["m"] == "v.oai.rad" for event in stick_events), stick_events
            assert stick_events[-1]["p"]["d"] == 0, stick_events
            time.sleep(.2)
            stick_metrics = status()
            checks["stick_render_max_us"] = stick_metrics["render_max_us"]
            checks["stick_input_interval_max_us"] = stick_metrics["input_interval_max_us"]
            checks["stick_profile"] = stick_metrics.get("ui_profile",{})
        finally:
            command("INPUT_TEST_END", "INPUT_TEST_ENDED")
        after = status()
        assert not after["input_capture"], after
        assert after["inputs_tx"] == before["inputs_tx"], (before["inputs_tx"],after["inputs_tx"])
        checks["host_inputs_before"] = before["inputs_tx"]
        checks["host_inputs_after"] = after["inputs_tx"]
        checks["capture_disabled_after_test"] = True
        checks["render_max_us"] = after["render_max_us"]
        checks["input_interval_max_us"] = after["input_interval_max_us"]
    result = {"firmware": after["firmware"], "checks": checks, "captured_events": events}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    return {"path": str(output.resolve()), **checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output",type=Path,nargs="?")
    parser.add_argument("--firmware",default="0.11.1",help="Exact expected firmware version")
    args=parser.parse_args()
    output=args.output or Path(f"artifacts/controls-qa-{args.firmware.removeprefix('tab5-micro-')}.json")
    print(json.dumps(verify(output,args.firmware),ensure_ascii=False,indent=2))
