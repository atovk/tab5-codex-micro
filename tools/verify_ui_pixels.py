#!/usr/bin/env python3
"""Compare dirty/full Tab5 frames under one isolated synthetic host preview.

The serial lock covers the entire capture, including cleanup. Captured touches
exercise UI pixels only; the fixed PREVIEW_HOST fixture is not live host proof.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from PIL import Image, ImageChops, ImageDraw

from control_layout import CURRENT as LAYOUT, FIRMWARE, VERSION
from tab5_transport import open_serial
from verify_host_feedback import chat_fixture, check_fixture, feedback_snapshot, preview
from verify_neon import Session, check_safety, require

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def verify(output: Path, port: str | None = None, timeout: float = 10) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    directory = output.parent
    payload = chat_fixture("standard", "plan", "read-only")
    report: dict = {
        "firmware": FIRMWARE, "synthetic_preview_only": True,
        "scope": "Captured touch/UI pixel verification with a fixed PREVIEW_HOST fixture; "
                 "not evidence of live host configuration, lighting, or executed host actions",
        "host_config_modified": False, "preview_fixture": payload,
        "captures": [], "success": False,
        "ignored": "Only the six small native-color LED regions copied into the preview",
    }
    error = None
    with open_serial(port, timeout) as connection:
        session = Session(connection, timeout)
        before = session.status()
        require(before["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not before["input_capture"], "Another capture session is active")
        require(not before["lighting_preview"], "Another preview session is active")
        require(not before.get("settings_open", False), "Close settings before pixel QA")
        require(before["theme"] in ("light", "dark"), "Unknown starting theme")
        report["before"] = before
        original = before["theme"].upper()
        capture_attempted = False
        capture_owned = False

        def touch(x: int, y: int, down: bool) -> None:
            require(capture_owned, "Refusing diagnostic touch outside owned capture")
            session.command(f"INPUT_TOUCH {x} {y} {int(down)}", "INPUT_TOUCH_OK")

        def screenshot(path: Path) -> dict:
            # This capture-only ACK renews the 60s watchdog before a potentially
            # slow binary transfer, without replacing the frozen host fixture.
            session.command("FX FREEZE", "FX FREEZE")
            return session.screenshot(path)

        try:
            session.command("MICRO", "MICRO")
            # An ACK timeout can still leave BEGIN accepted. The locked baseline
            # was inactive, so cleanup may claim only capture started by this run.
            capture_attempted = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            capture_owned = True
            # Exercise the new two-row title masks while pressing/releasing
            # every key. Lighting remains the locked baseline; title text is
            # isolated in the preview and never sent through TITLES.
            labels = ("中文标题需要两行完整显示", "Code review and design updates",
                      "旋钮和摇杆保持流畅响应", "没有省略号的长聊天标题",
                      "完整标题自动换行和滚动显示", "A long title for the final Agent")
            title_fixtures = [{"c": slot["c"], "b": slot["b"] / 255,
                               "e": slot["e"], "s": slot["s"] / 255,
                               "title": label}
                              for slot, label in zip(before["slots"], labels)]
            session.preview(title_fixtures)
            report["synthetic_title_fixtures"] = title_fixtures
            preview(session, payload)
            current = session.status()
            check_safety(current, before, session.events)
            check_fixture(current, payload, "听写录音灯")
            require(current["effects_frozen"] is True, "Capture effects are not frozen")
            fixed_feedback = feedback_snapshot(current)
            # Preview owns a copy of the model. Native changes and the live
            # configuration's 7.5s TTL cannot change this comparison scene.
            time.sleep(1.3)

            # Exercise both header rebuild/release paths, returning to the
            # starting theme before checking the 13 native switch shadows.
            for _ in range(2):
                touch(1015, 5, True); time.sleep(.1)
                touch(1015, 5, False); time.sleep(.1)
            targets = [(125 + 206 * i, 151) for i in range(6)]
            targets += [(103, 617), (265, 617), (427, 617), (589, 617),
                        (761, 617), (931, 617), (1140, 617)]
            for x, y in targets:
                touch(x, y, True); time.sleep(.06)
                touch(x, y, False); time.sleep(.06)
            report["pressed_and_released_keys"] = len(targets)

            touch(LAYOUT.dial_x, LAYOUT.dial_y, True)
            time.sleep(.65)
            report["captures"].append(screenshot(
                directory / f"tab5-longpress-{VERSION}.png"))
            touch(LAYOUT.dial_x, LAYOUT.dial_y, False)
            touch(LAYOUT.stick_x, LAYOUT.stick_y, True)
            touch(LAYOUT.stick_x + 64, LAYOUT.stick_y + 64, True)
            time.sleep(.1)
            report["captures"].append(screenshot(
                directory / f"tab5-stick-{VERSION}.png"))
            touch(LAYOUT.stick_x + 64, LAYOUT.stick_y + 64, False)
            time.sleep(.2)
            # Exercise real-state invalidation paths as an isolated preview:
            # Standard -> Fast, Plan -> default and read-only -> Full access.
            payload = chat_fixture("fast", "default", "full-access")
            preview(session, payload)
            time.sleep(.3)
            current = session.status()
            check_fixture(current, payload, "听写录音灯")
            fixed_feedback = feedback_snapshot(current)
            report["chat_state_transition"] = payload["config"]["chat"]
            dirty = directory / f"tab5-dirty-rest-{VERSION}.png"
            report["captures"].append(screenshot(dirty))

            # A theme round trip reconstructs the same frozen preview from
            # scratch. Do not refresh PREVIEW_HOST between the two frames.
            opposite = "DARK" if original == "LIGHT" else "LIGHT"
            session.command(f"THEME {opposite}", f"THEME {opposite}")
            time.sleep(1.3)
            session.command(f"THEME {original}", f"THEME {original}")
            time.sleep(1.3)
            fresh = directory / f"tab5-full-rest-{VERSION}.png"
            report["captures"].append(screenshot(fresh))
            current = session.status()
            check_safety(current, before, session.events)
            check_fixture(current, payload, "听写录音灯")
            require(feedback_snapshot(current) == fixed_feedback,
                    "Synthetic host feedback changed during framebuffer comparison")
            require(current["effects_frozen"] is True, "Effects became live during comparison")
            require(current["theme"] == before["theme"], "Theme round trip did not restore theme")

            with Image.open(dirty) as first, Image.open(fresh) as second:
                require(first.size == second.size == (1280, 720), "Unexpected screenshot size")
                difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
            report["raw_changed_rest_pixels"] = sum(
                pixel != (0, 0, 0) for pixel in difference.get_flattened_data())
            # Keep the existing narrow exclusion for native slot LED pulses;
            # all header/config text and every other pixel must match exactly.
            painter = ImageDraw.Draw(difference)
            ignored = []
            for index in range(6):
                x = 28 + 206 * index + 194 - 19
                box = (x - 7, 120, x + 7, 135)
                ignored.append(box)
                painter.rectangle(box, fill=(0, 0, 0))
            changed = sum(pixel != (0, 0, 0) for pixel in difference.get_flattened_data())
            bounds = difference.getbbox()
            difference_path = directory / f"tab5-dirty-difference-{VERSION}.png"
            difference.save(difference_path)
            report.update(changed_rest_pixels=changed, changed_rest_bounds=bounds,
                          ignored_rectangles=ignored, difference_path=str(difference_path))
            require(changed == 0, f"Dirty/full mismatch: {changed} pixels, bounds={bounds}")
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
                if capture_attempted:
                    try:
                        session.command(f"THEME {original}", f"THEME {original}")
                    except Exception as failure:
                        cleanup_errors.append(f"Restore theme: {failure}")
                    try:
                        # END releases a held test gesture, clears only this
                        # preview, and restores the pre-BEGIN effects setting.
                        if session.status()["input_capture"] is True:
                            session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                    except Exception as failure:
                        cleanup_errors.append(f"INPUT_TEST_END: {failure}")
                try:
                    after = session.status()
                    report["after"] = after
                    check_safety(after, before, session.events, capture=False)
                    require(not after["lighting_preview"], "Preview was not cleared")
                    require(after["theme"] == before["theme"], "Original theme was not restored")
                    require(after["effects_frozen"] == before["effects_frozen"],
                            "Original effects state was not restored")
                    report.update(theme=after["theme"], host_inputs_before=before["inputs_tx"],
                                  host_inputs_after=after["inputs_tx"])
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
                        default=ARTIFACTS / f"pixels-qa-{VERSION}.json")
    args = parser.parse_args()
    result = verify(args.output, args.port, args.timeout)
    print(json.dumps({"path": str(args.output.resolve()), "success": result["success"],
                      "changed_rest_pixels": result["changed_rest_pixels"],
                      "host_inputs": result["host_inputs_after"],
                      "synthetic_preview_only": True}, indent=2))


if __name__ == "__main__":
    main()
