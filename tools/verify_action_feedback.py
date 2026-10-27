#!/usr/bin/env python3
"""Verify synthetic 1.2-second action-receipt pixels on Tab5 firmware 0.11.0.

One locked CDC session captures baseline, sent, unsent and expired frames. Only
capture-gated PREVIEW_HOST fixtures are used: no touch, native HID input, live
HOST_CONFIG change or desktop action is exercised. Pixel differences demonstrate
local receipt rendering, not successful USB delivery or application execution.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from PIL import Image, ImageChops

from tab5_transport import open_serial
from verify_host_feedback import FIRMWARE, VERSION, check_safety, fixture, preview
from verify_neon import Session, require


ROOT = Path(__file__).resolve().parents[1]
# src/micro_ui.cpp buttons[6]: ACT06 / fixture feedback[0]. PIL uses exclusive
# right/bottom coordinates, so this is exactly the complete 150x150 key rectangle.
KEY_RECT = (28, 542, 150, 150)
KEY_BOX = (28, 542, 178, 692)
RECEIPT_SECONDS = 1.2
RENDER_WAIT = .08


def compare(first: Path, second: Path) -> dict:
    """Count exact RGB pixel changes, without saving additional screenshots."""
    with Image.open(first) as a, Image.open(second) as b:
        require(a.size == b.size == (1280, 720), "Unexpected capture dimensions")
        difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    roi = difference.crop(KEY_BOX)
    changed = lambda image: sum(pixel != (0, 0, 0) for pixel in image.getdata())
    key_changes = changed(roi)
    total_changes = changed(difference)
    return {"changed_key_pixels": key_changes,
            "changed_outside_key_pixels": total_changes - key_changes}


def verify(output: Path, port: str | None = None, timeout: float = 10) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"firmware": FIRMWARE, "synthetic_preview_only": True,
              "evidence_scope": "Synthetic receipt pixels only; native USB delivery and "
                                "application execution are not exercised or confirmed",
              "host_config_modified": False, "native_input_exercised": False,
              "key_rect": dict(zip(("x", "y", "width", "height"), KEY_RECT)),
              "receipt_ms": 1200, "captures": [], "checks": {}, "success": False}
    error = None
    with open_serial(port, timeout) as connection:
        session = Session(connection, timeout)
        before = session.status()
        require(before["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not before["input_capture"], "Another capture session is active")
        require(not before["lighting_preview"], "Another preview session is active")
        require(not before.get("settings_open", False), "Close settings before testing receipts")
        require(before["theme"] in ("light", "dark"), "Unknown starting theme")
        report["before"] = before
        began = False
        try:
            # Set before BEGIN so an ACK timeout still receives gated cleanup.
            began = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            current = session.status()
            check_safety(current, before, session, capture=True)
            require(current["effects_frozen"] is True, "Capture did not freeze effects")

            # PREVIEW_HOST copies the live slots once and then retains that
            # preview model. Subsequent fixtures alter no Agent state or theme.
            paths = {}
            latest_feedback_at = None
            for name, feedback in (("baseline", None), ("sent", True), ("unsent", False)):
                payload = fixture()
                payload["feedback"][0] = feedback
                preview(session, payload)
                applied_at = time.monotonic()
                if feedback is not None:
                    latest_feedback_at = applied_at
                time.sleep(RENDER_WAIT)
                requested_at = time.monotonic()
                if feedback is not None:
                    require(requested_at - applied_at < RECEIPT_SECONDS - .1,
                            "Feedback capture was requested too late")
                path = output.parent / f"tab5-action-feedback-{name}-{VERSION}.png"
                # SCREEN copies the canvas at command start; receipt expiry
                # during the potentially long transfer cannot alter this frame.
                capture = session.screenshot(path)
                capture.update({"case": name, "feedback_slot": 0, "feedback": feedback,
                                "source": "actual_framebuffer_with_synthetic_host_fixture",
                                "request_after_fixture_ack_ms": round(
                                    (requested_at - applied_at) * 1000, 3),
                                "stream_elapsed_ms": round(
                                    (time.monotonic() - requested_at) * 1000, 3)})
                report["captures"].append(capture)
                paths[name] = path
                current = session.status()
                check_safety(current, before, session, capture=True)
                require(current["lighting_preview"] is True, "Preview ended during capture")
                require(current["effects_frozen"] is True, "Decorative effects resumed")
                require(current["theme"] == before["theme"], "Theme changed during test")

            require(latest_feedback_at is not None, "No feedback fixture was applied")
            # Usually SCREEN already takes longer than the receipt lifetime.
            # Keep the test correct on faster transports too, with a render margin.
            rest_at = latest_feedback_at + RECEIPT_SECONDS + RENDER_WAIT
            time.sleep(max(0, rest_at - time.monotonic()))
            requested_at = time.monotonic()
            rest_path = output.parent / f"tab5-action-feedback-expired-{VERSION}.png"
            capture = session.screenshot(rest_path)
            capture.update({"case": "expired", "feedback_slot": 0,
                            "source": "actual_framebuffer_with_synthetic_host_fixture",
                            "request_after_fixture_ack_ms": round(
                                (requested_at - latest_feedback_at) * 1000, 3)})
            report["captures"].append(capture)
            current = session.status()
            check_safety(current, before, session, capture=True)
            require(current["lighting_preview"] is True, "Preview ended before expiry check")
            require(current["effects_frozen"] is True, "Decorative effects resumed")
            require(current["theme"] == before["theme"], "Theme changed during test")

            for name, first, second, different in (
                ("baseline_to_sent", paths["baseline"], paths["sent"], True),
                ("baseline_to_unsent", paths["baseline"], paths["unsent"], True),
                ("sent_to_unsent", paths["sent"], paths["unsent"], True),
                ("expired_to_baseline", rest_path, paths["baseline"], False),
            ):
                result = compare(first, second)
                report["checks"][name] = result
                require((result["changed_key_pixels"] > 0) is different,
                        f"{name}: unexpected key pixel difference {result}")
        except Exception as failure:
            error = failure
            report["error"] = f"{type(failure).__name__}: {failure}"
        finally:
            cleanup_errors = []
            stream_ready = True
            try:
                session.recover_frame()
            except Exception as failure:
                stream_ready = False
                cleanup_errors.append(f"Framebuffer recovery: {failure}")
            if stream_ready:
                if began:
                    try:
                        # Only end our gated capture. END clears the preview
                        # and restores the effects setting from before BEGIN.
                        if session.status()["input_capture"] is True:
                            session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                    except Exception as failure:
                        cleanup_errors.append(f"INPUT_TEST_END: {failure}")
                try:
                    after = session.status()
                    report["after"] = after
                    check_safety(after, before, session, capture=False)
                    require(not after["lighting_preview"], "Preview was not cleared")
                    require(after["effects_frozen"] == before["effects_frozen"],
                            "Original effects state was not restored")
                    require(after["theme"] == before["theme"], "Original theme changed")
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
                        default=ROOT / "artifacts" / f"action-feedback-qa-{VERSION}.json")
    args = parser.parse_args()
    result = verify(args.output, args.port, args.timeout)
    print(json.dumps({"success": result["success"], "synthetic_preview_only": True,
                      "output": str(args.output.resolve()), "checks": result["checks"],
                      "captures": [item["path"] for item in result["captures"]]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
