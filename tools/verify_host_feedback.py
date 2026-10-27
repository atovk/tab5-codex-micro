#!/usr/bin/env python3
"""Verify isolated host feedback previews without sending native inputs.

Uses only PREVIEW_HOST fixtures; never modifies HOST_CONFIG or daemon metadata.
The two optional captures are synthetic demonstrations, not live host evidence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time

from tab5_transport import open_serial
from verify_device import read_line
from verify_neon import Session, require


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.10.2"
FIRMWARE = f"tab5-micro-{VERSION}"
TIERS = ("unknown", "standard", "fast", "ultrafast")
ENCODERS = ("unknown", "composer-navigation", "reasoning", "conversation-scroll", "custom")
VOICES = ("unknown", "push-to-talk", "realtime")
ACTIONS = ("unknown", "none", "fast", "approve", "reject", "fork", "voice", "aux",
           "send", "plan", "sidebar", "back", "forward", "custom")
DEFAULT_ACTIONS = ["fast", "approve", "reject", "fork", "voice", "aux", "send"]


def fixture(color: int = 0x2E8B57, effect: int = 2, magic: int = 0,
            tier: str = "fast", encoder: str = "reasoning",
            voice: str = "push-to-talk", actions: list[str] | None = None) -> dict:
    return {
        "rgb": {
            "keys": {"c": 0x304FFE, "b": 0.8, "e": 1, "s": 0.0, "m": 0},
            "ambient": {"c": color, "b": 1.0, "e": effect, "s": 0.4, "m": magic},
        },
        "config": {"v": 1, "seq": 1, "source": "local-config", "tier": tier,
                   "encoder": encoder, "voice": voice,
                   "actions": list(DEFAULT_ACTIONS if actions is None else actions)},
        "feedback": [None] * 7,
    }


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def chat_fixture(speed="fast", mode="plan", permission="full-access") -> dict:
    value = fixture()
    value["config"]["chat"] = {"source": "desktop-ipc", "scope": "explicit-target",
                               "thread_id": "00000000-0000-0000-0000-000000000001",
                               "title": "合成状态演示", "speed": speed, "mode": mode,
                               "permission": permission}
    return value


def feedback_snapshot(status: dict) -> dict:
    value = deepcopy(status["host_feedback"])
    # This age belongs to the untouched live metadata, not the preview fixture.
    value.pop("config_age_ms", None)
    return value


def check_safety(current: dict, baseline: dict, session: Session, capture: bool) -> None:
    require(current["inputs_tx"] == baseline["inputs_tx"], "Native input counter changed")
    require(current["captured_inputs"] == baseline["captured_inputs"],
            "Unexpected captured input during state-only QA")
    require(not session.events, "Unexpected input event during state-only QA")
    require(current["input_capture"] is capture, "Unexpected capture state")


def expected_zone(value: dict) -> dict:
    # Inputs are nonnegative; firmware uses lroundf rather than Python ties-to-even.
    return {key: int(value[key] * 255 + 0.5) if key in ("b", "s") else value[key]
            for key in ("c", "b", "e", "s", "m")}


def check_fixture(current: dict, payload: dict, signal: str) -> None:
    config, actual = payload["config"], current["host_feedback"]
    require(current["lighting_preview"] is True, "Host preview was not activated")
    require(actual["config_fresh"] is (config["source"] == "local-config"),
            "Configuration freshness/source mismatch")
    expected = {"default_tier": TIERS.index(config["tier"]),
                "encoder": ENCODERS.index(config["encoder"]),
                "voice": VOICES.index(config["voice"]),
                "actions": [ACTIONS.index(action) for action in config["actions"]],
                "rgb_known": True, "signal": signal}
    expected.update({key: expected_zone(payload["rgb"][key]) for key in ("keys", "ambient")})
    for key, value in expected.items():
        require(actual.get(key) == value,
                f"Host feedback {key}: {actual.get(key)!r}, expected {value!r}")
    chat = config.get("chat", {"source": "unavailable", "scope": "explicit-target",
                              "thread_id": "", "title": "", "speed": "unknown",
                              "mode": "unknown", "permission": "unknown"})
    require(actual["chat"] == {**chat, "fresh": chat["source"] == "desktop-ipc"},
            "Live chat preview did not match its explicit fields")


def preview(session: Session, payload: dict) -> None:
    session.command("PREVIEW_HOST " + encode(payload), "PREVIEW_HOST_OK")


def expect_rejection(session: Session, payload: str) -> None:
    """Read the expected ERROR directly, since Session.line raises on errors."""
    session.send("PREVIEW_HOST " + payload)
    deadline = time.monotonic() + session.timeout
    while time.monotonic() < deadline:
        raw = read_line(session.connection, deadline)
        if raw.startswith(b"INPUT_EVENT "):
            session.events.append(json.loads(raw.removeprefix(b"INPUT_EVENT ")))
            continue
        if raw == b"PREVIEW_HOST_ERROR":
            return
        require(raw != b"PREVIEW_HOST_OK", "Invalid/outside-capture preview was accepted")
        require(b"ERROR" not in raw, f"Unexpected diagnostic error: {raw!r}")
    raise TimeoutError("No PREVIEW_HOST_ERROR acknowledgement")


def valid_cases() -> list[tuple[str, dict, str]]:
    cases = [("recording", fixture(), "听写录音灯"),
             ("processing", fixture(0xFFFFFF), "听写处理灯"),
             ("solid_white_is_not_completion", fixture(0xFFFFFF, effect=1), "主机灯效"),
             ("dial_selection", fixture(0x00FF4C), "拨轮选择"),
             ("unknown_magic", fixture(magic=7), "主机灯效"),
             ("working_or_selection", fixture(0x304FFE), "运行／选择")]
    for tier, encoder, voice in (("standard", "composer-navigation", "push-to-talk"),
                                 ("fast", "reasoning", "realtime"),
                                 ("ultrafast", "conversation-scroll", "push-to-talk")):
        cases.append((f"config_{tier}", fixture(tier=tier, encoder=encoder, voice=voice),
                      "听写录音灯"))
    cases.append(("custom_and_disabled_actions",
                  fixture(encoder="custom", actions=["custom", "none", "plan", "sidebar",
                                                      "back", "forward", "custom"]),
                  "听写录音灯"))
    unavailable = fixture(tier="unknown", encoder="unknown", voice="unknown",
                          actions=["unknown"] * 7)
    unavailable["config"]["source"] = "unavailable"
    cases.append(("unavailable", unavailable, "听写录音灯"))
    feedback = fixture()
    feedback["feedback"] = [True, False, None, True, False, None, True]
    cases.append(("synthetic_send_feedback", feedback, "听写录音灯"))
    for speed, mode, permission in (("fast", "plan", "full-access"),
                                     ("standard", "default", "workspace-write"),
                                     ("ultrafast", "plan", "read-only"),
                                     ("default", "default", "custom"),
                                     ("unknown", "unknown", "unknown")):
        cases.append((f"chat_{speed}_{mode}_{permission}",
                      chat_fixture(speed, mode, permission), "听写录音灯"))
    cleared = chat_fixture("unknown", "unknown", "unknown")
    cleared["config"]["chat"].update(source="unavailable", thread_id="", title="")
    cases.append(("chat_unavailable_clears", cleared, "听写录音灯"))
    return cases


def invalid_cases() -> list[tuple[str, str]]:
    cases = [("malformed_json", "{"), ("top_level_array", "[]")]

    def changed(name: str, path: tuple[str, ...], value: object) -> None:
        payload = fixture()
        target = payload
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        cases.append((name, encode(payload)))

    for name, path, value in (
        ("rgb_wrong_type", ("rgb",), []),
        ("zone_wrong_type", ("rgb", "ambient"), "green"),
        ("color_wrong_type", ("rgb", "ambient", "c"), True),
        ("color_out_of_range", ("rgb", "ambient", "c"), 0x1000000),
        ("color_negative", ("rgb", "ambient", "c"), -1),
        ("brightness_wrong_type", ("rgb", "ambient", "b"), "1"),
        ("brightness_out_of_range", ("rgb", "ambient", "b"), 1.01),
        ("speed_negative", ("rgb", "ambient", "s"), -0.01),
        ("effect_wrong_type", ("rgb", "ambient", "e"), "2"),
        ("effect_out_of_range", ("rgb", "ambient", "e"), 256),
        ("magic_wrong_type", ("rgb", "ambient", "m"), False),
        ("config_wrong_type", ("config",), []),
        ("version_wrong_type", ("config", "v"), True),
        ("sequence_wrong_type", ("config", "seq"), "2"),
        ("sequence_negative", ("config", "seq"), -1),
        ("source_wrong_type", ("config", "source"), 1),
        ("source_invalid", ("config", "source"), "native-rpc"),
        ("unavailable_with_known_values", ("config", "source"), "unavailable"),
        ("tier_invalid", ("config", "tier"), "turbo"),
        ("encoder_wrong_type", ("config", "encoder"), False),
        ("voice_invalid", ("config", "voice"), "recording"),
        ("actions_wrong_type", ("config", "actions"), {}),
        ("actions_missing_slot", ("config", "actions"), DEFAULT_ACTIONS[:-1]),
        ("actions_extra_slot", ("config", "actions"), DEFAULT_ACTIONS + ["none"]),
        ("actions_invalid_value", ("config", "actions"), DEFAULT_ACTIONS[:-1] + ["delete"]),
        ("actions_wrong_value_type", ("config", "actions"), DEFAULT_ACTIONS[:-1] + [True]),
        ("feedback_wrong_type", ("feedback",), {}),
        ("feedback_missing_slot", ("feedback",), [None] * 6),
        ("feedback_invalid_value", ("feedback",), [None] * 6 + [1]),
    ):
        changed(name, path, value)
    # Exercise the parser's nonstandard-number path with literal NaN, not a string.
    cases.append(("nonfinite_brightness",
                  encode(fixture()).replace('"b":1.0', '"b":NaN')))
    for field, bad in (("source", "local-config"), ("source", "desktop-ipc\x00suffix"),
                       ("scope", "current-chat"), ("thread_id", "recent"),
                       ("thread_id", "00000000-0000-0000-0000-000000000001\x00"),
                       ("title", "x" * 192), ("title", "bad\nline"),
                       ("title", "bad\x00title"), ("speed", "priority"),
                       ("mode", "planning"), ("permission", "never"),
                       ("permission", None), ("permission", True)):
        value = chat_fixture()
        value["config"]["chat"][field] = bad
        cases.append((f"chat_invalid_{field}_{len(cases)}", encode(value)))
    value = chat_fixture()
    value["config"]["chat"]["source"] = "unavailable"
    cases.append(("chat_unavailable_rejects_known_fields", encode(value)))
    return cases


def verify(output: Path, port: str | None = None, timeout: float = 10,
           screenshots: bool = True) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"firmware": FIRMWARE, "synthetic_preview_only": True,
              "host_config_modified": False, "valid_cases": [], "invalid_cases": [],
              "captures": [], "success": False}
    error = None
    with open_serial(port, timeout) as connection:
        session = Session(connection, timeout)
        before = session.status()
        require(before["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not before["input_capture"], "Another capture session is active")
        require(not before["lighting_preview"], "Another preview session is active")
        require(not before.get("settings_open", False), "Close settings before capturing state demos")
        require(before["theme"] in ("light", "dark"), "Unknown starting theme")
        report["before"] = before
        capture_started = False
        try:
            expect_rejection(session, encode(fixture()))
            outside = session.status()
            check_safety(outside, before, session, capture=False)
            require(not outside["lighting_preview"], "Outside-capture rejection activated preview")
            report["outside_capture_rejected"] = True
            # Set this before the command so ACK timeouts still trigger gated cleanup.
            capture_started = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            for sequence, (name, payload, signal) in enumerate(valid_cases(), 1):
                payload["config"]["seq"] = sequence
                preview(session, payload)
                current = session.status()
                check_safety(current, before, session, capture=True)
                check_fixture(current, payload, signal)
                report["valid_cases"].append({"name": name, "payload": payload,
                                              "host_feedback": current["host_feedback"]})
                if name == "synthetic_send_feedback":
                    report["valid_cases"][-1]["scope"] = (
                        "Fixture acceptance only; per-key receipt pixels are not verified, "
                        "and no native host input was sent")

            # Freeze a complete known baseline, then reject every mutation atomically.
            preview(session, fixture())
            stable = feedback_snapshot(session.status())
            for name, payload in invalid_cases():
                expect_rejection(session, payload)
                current = session.status()
                check_safety(current, before, session, capture=True)
                require(current["lighting_preview"] is True, "Rejected data cleared preview")
                require(feedback_snapshot(current) == stable,
                        f"Rejected fixture {name} changed host feedback")
                report["invalid_cases"].append({"name": name, "rejected": True,
                                                "state_unchanged": True})

            if screenshots:
                for theme in ("light", "dark"):
                    session.command(f"THEME {theme.upper()}", f"THEME {theme.upper()}")
                    preview(session, fixture())
                    # Theme redraw is deferred; wait before requesting its framebuffer.
                    time.sleep(1.3)
                    current = session.status()
                    check_safety(current, before, session, capture=True)
                    check_fixture(current, fixture(), "听写录音灯")
                    require(current["theme"] == theme, "Theme was not applied")
                    report["captures"].append(session.screenshot(
                        output.parent / f"tab5-state-demo-{theme}-{VERSION}.png"))
        except Exception as failure:
            error = failure
            report["error"] = f"{type(failure).__name__}: {failure}"
        finally:
            cleanup_errors = []
            stream_ready = True
            try:
                session.recover_frame()
            except Exception as failure:
                cleanup_errors.append(f"Framebuffer recovery: {failure}")
                stream_ready = False
            if stream_ready:
                if capture_started:
                    try:
                        session.command(f"THEME {before['theme'].upper()}",
                                        f"THEME {before['theme'].upper()}")
                    except Exception as failure:
                        cleanup_errors.append(f"Restore theme: {failure}")
                    try:
                        # END clears previews and restores the pre-BEGIN effects setting.
                        # A rejected BEGIN must not release a real desktop key.
                        if session.status()["input_capture"] is True:
                            session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                    except Exception as failure:
                        cleanup_errors.append(f"INPUT_TEST_END: {failure}")
                try:
                    after = session.status()
                    report["after"] = after
                    check_safety(after, before, session, capture=False)
                    require(not after["lighting_preview"], "Preview was not cleared")
                    require(after["theme"] == before["theme"], "Original theme was not restored")
                    require(after["effects_frozen"] == before["effects_frozen"],
                            "Original effects state was not restored")
                except Exception as failure:
                    cleanup_errors.append(str(failure))
            else:
                cleanup_errors.append("Capture watchdog must clear the unrecovered stream/session")
            report["cleanup_errors"] = cleanup_errors
            report["captured_events"] = session.events
            report["success"] = error is None and not cleanup_errors
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if error is not None:
        raise error
    require(report["success"], f"Cleanup failed: {report['cleanup_errors']}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts" / f"host-feedback-qa-{VERSION}.json")
    parser.add_argument("--no-screenshots", action="store_true")
    args = parser.parse_args()
    result = verify(args.output, args.port, args.timeout, not args.no_screenshots)
    print(json.dumps({"success": result["success"], "output": str(args.output.resolve()),
                      "valid_cases": len(result["valid_cases"]),
                      "rejected_cases": len(result["invalid_cases"]),
                      "captures": [item["path"] for item in result["captures"]]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
