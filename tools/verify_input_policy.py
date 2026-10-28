#!/usr/bin/env python3
"""Capture-only regression for release-to-activate and native hold semantics.

The device must run the exact current 0.13.1 firmware. Synthetic PREVIEW_HOST
mappings exercise its real touch/event path while INPUT_TEST suppresses all
native host inputs. No application state or real chat titles enter the report.
Stop the title companion before running this tool; restart it afterwards.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time

from control_layout import CURRENT, check_cpp_layout
from tab5_transport import open_serial, selected_serial
from verify_host_feedback import ACTIONS, fixture, preview
from verify_neon import Session, check_safety, require


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = "tab5-micro-0.13.1"
HOLD_SECONDS = .65
SAFE_STATUS_FIELDS = (
    "firmware", "inputs_tx", "captured_inputs", "input_capture",
    "lighting_preview", "effects_frozen", "theme", "language",
    "active_transport", "usb_mounted", "settings_open", "playing",
)


def status_projection(status: dict) -> dict:
    """Never persist the full STATUS payload, which may contain real titles."""
    return {name: status.get(name) for name in SAFE_STATUS_FIELDS}


def key_event(key: str, action: int) -> dict:
    return {"m": "v.oai.hid", "p": {"k": key, "act": action}}


def unavailable_fixture() -> dict:
    value = fixture(tier="unknown", encoder="unknown", voice="unknown",
                    actions=["unknown"] * 7)
    value["config"]["source"] = "unavailable"
    return value


def verify(output: Path, *, device_serial: str, port: str | None = None,
           timeout: float = 10) -> dict:
    identity = selected_serial(device_serial)
    require(identity is not None, "An exact Tab5 device serial is required")
    require(timeout > 0, "Serial timeout must be positive")
    check_cpp_layout()
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "firmware": FIRMWARE, "device_serial": identity,
        "capture_only": True, "synthetic_preview_only": True,
        "scope": "Real device touch and serialization under isolated mappings; "
                 "not evidence of desktop execution",
        "host_config_modified": False, "checks": [], "success": False,
    }
    error: Exception | None = None
    with open_serial(port, timeout, serial_number=identity) as connection:
        session = Session(connection, timeout)
        before = session.status()
        require(before.get("firmware") == FIRMWARE, f"Expected {FIRMWARE}")
        require(before.get("input_capture") is False, "Another capture session is active")
        require(before.get("lighting_preview") is False, "Another preview session is active")
        require(not before.get("settings_open") and not before.get("playing"),
                "Return to the console before input-policy QA")
        require(not session.events, "Unexpected input before capture")
        report["before"] = status_projection(before)
        capture_attempted = False
        capture_owned = False
        sequence = 0

        def checked_status() -> dict:
            current = session.status()
            check_safety(current, before, session.events, capture=True)
            require(current.get("effects_frozen") is True, "Capture effects are not frozen")
            return current

        def touch(x: int, y: int, down: bool) -> None:
            require(capture_owned, "Refusing touch without owned capture")
            session.command(f"INPUT_TOUCH {x} {y} {int(down)}", "INPUT_TOUCH_OK")
            checked_status()

        def set_mapping(value: dict) -> None:
            nonlocal sequence
            require(capture_owned, "Refusing mapping preview without owned capture")
            value = deepcopy(value)
            sequence += 1
            value["config"]["seq"] = sequence
            preview(session, value)
            current = checked_status()
            actual = current.get("host_feedback", {})
            require(current.get("lighting_preview") is True, "Host preview is inactive")
            require(actual.get("config_fresh") is (value["config"]["source"] == "local-config"),
                    "Preview freshness does not match its source")
            expected = [ACTIONS.index(action) for action in value["config"]["actions"]]
            require(actual.get("actions") == expected, "Preview action mapping was not applied")

        def expect(start: int, expected: list[dict], label: str) -> None:
            checked_status()
            require(session.events[start:] == expected,
                    f"{label}: captured events differ from the expected key sequence")

        def record(name: str, start: int, **details) -> None:
            report["checks"].append({"name": name, "passed": True,
                                     "events": session.events[start:], **details})

        try:
            # A timeout can follow an accepted BEGIN. The baseline was inactive;
            # cleanup checks whether this attempt acquired the capture session.
            capture_attempted = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            capture_owned = True
            checked_status()
            require(not session.events, "Unexpected input while entering capture")
            standard = fixture()
            set_mapping(standard)

            for name, key, x, y in (
                ("approve", "ACT07", 265, 617),
                ("reject", "ACT08", 427, 617),
                ("send", "ACT12", 1140, 617),
            ):
                start = len(session.events)
                touch(x, y, True)
                expect(start, [], f"{name} must defer its down edge")
                touch(x, y, True)
                expect(start, [], f"{name} must stay deferred while held")
                touch(x, y, False)
                expect(start, [key_event(key, 1), key_event(key, 0)],
                       f"{name} inside release")
                record(f"{name}_inside_release", start)

                start = len(session.events)
                touch(x, y, True)
                touch(CURRENT.chat_x + CURRENT.chat_width // 2, 400, True)
                touch(x, y, True)
                touch(x, y, False)
                expect(start, [], f"{name} must stay cancelled after reentry")
                record(f"{name}_exit_and_reenter_cancelled", start)

                start = len(session.events)
                touch(x, y, True)
                touch(CURRENT.chat_x + CURRENT.chat_width // 2, 400, False)
                expect(start, [], f"{name} outside release must cancel")
                record(f"{name}_outside_release_cancelled", start)

            start = len(session.events)
            touch(265, 617, True)
            expect(start, [], "Approve before mapping replacement")
            changed = deepcopy(standard)
            changed["config"]["actions"][1] = "send"
            set_mapping(changed)
            touch(265, 617, False)
            expect(start, [], "Changed action with the same release mask must cancel")
            record("same_mask_mapping_change_cancelled", start,
                   mapping_before="approve", mapping_after="send")
            set_mapping(standard)

            start = len(session.events)
            touch(265, 617, True)
            expect(start, [], "Approve before metadata becomes unavailable")
            set_mapping(unavailable_fixture())
            touch(265, 617, False)
            expect(start, [], "Unavailable mapping must cancel the pending action")
            record("unavailable_mapping_cancelled", start)
            set_mapping(standard)

            for name, key, x, y in (
                ("voice", "ACT10", 761, 617),
                ("encoder", "ENC", CURRENT.dial_x, CURRENT.dial_y),
            ):
                start = len(session.events)
                touch(x, y, True)
                expect(start, [key_event(key, 1)], f"{name} needs an immediate native down")
                time.sleep(HOLD_SECONDS)
                touch(x, y, True)
                expect(start, [key_event(key, 1)], f"{name} hold must not duplicate down")
                touch(x, y, False)
                expect(start, [key_event(key, 1), key_event(key, 0)],
                       f"{name} needs a balanced native release")
                record(f"{name}_native_hold_preserved", start,
                       minimum_hold_ms=round(HOLD_SECONDS * 1000))
        except Exception as failure:
            error = failure
            report["error"] = f"{type(failure).__name__}: {failure}"
        finally:
            cleanup_errors = []
            if capture_attempted:
                try:
                    if session.status().get("input_capture") is True:
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                except Exception as failure:
                    cleanup_errors.append(f"INPUT_TEST_END: {failure}")
            try:
                after = session.status()
                report["after"] = status_projection(after)
                check_safety(after, before, session.events, capture=False)
                require(after.get("lighting_preview") is False, "Preview was not cleared")
                require(after.get("effects_frozen") == before.get("effects_frozen"),
                        "Original effects setting was not restored")
                require(after.get("theme") == before.get("theme"), "Theme changed during QA")
                require(after.get("language") == before.get("language"), "Language changed during QA")
            except Exception as failure:
                cleanup_errors.append(str(failure))
            report["cleanup_errors"] = cleanup_errors
            report["captured_events"] = session.events
            report["success"] = error is None and not cleanup_errors
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if error is not None:
        raise error
    require(report["success"], "Input-policy QA cleanup failed; inspect its JSON report")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-serial", required=True,
                        help="Exact TAB5-0123456789AB USB chip identity")
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts" / "input-policy-qa-0.13.1.json")
    args = parser.parse_args()
    result = verify(args.output, device_serial=args.device_serial,
                    port=args.port, timeout=args.timeout)
    print(json.dumps({"success": result["success"], "checks": len(result["checks"]),
                      "captured_events": len(result["captured_events"]),
                      "output": str(args.output.expanduser().resolve())}, indent=2))


if __name__ == "__main__":
    main()
