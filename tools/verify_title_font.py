#!/usr/bin/env python3
"""Capture packed-font title samples and both actual live Tab5 themes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from build_title_font import DEFAULT_OUTPUT, inspect_asset
from tab5_transport import open_serial
from verify_host_feedback import chat_fixture, preview
from verify_neon import Session, require

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.10.2"


def verify(output: Path) -> dict:
    asset = inspect_asset(DEFAULT_OUTPUT.read_bytes())
    report = {"firmware": f"tab5-micro-{VERSION}", "asset": asset,
              "samples": [], "live_captures": [], "success": False}
    output.parent.mkdir(parents=True, exist_ok=True)

    def check_font(status):
        require(status["firmware"] == report["firmware"], "Unexpected firmware")
        require(status["title_font"] == {"loaded": True, "glyphs": asset["glyph_count"],
                                         "asset_bytes": asset["file_size"]},
                "Complete title font was not loaded")

    try:
        with open_serial(timeout=10) as connection:
            session = Session(connection, 10)
            before = session.status()
            check_font(before)
            require(not before["input_capture"] and not before["lighting_preview"],
                    "Another diagnostic session is active")
            original = before["theme"].upper()
            report["before"] = before
            attempted = False
            try:
                attempted = True
                session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
                for index, (title, theme) in enumerate((
                    ("龘麤鬱 · café", "LIGHT"),
                    ("日本語 한국어", "DARK"),
                    ("测试 🚀 𠀀 j", "DARK"),
                )):
                    session.command(f"THEME {theme}", f"THEME {theme}")
                    payload = chat_fixture("fast", "default", "full-access")
                    payload["config"]["chat"]["title"] = title
                    preview(session, payload)
                    time.sleep(.5)
                    state = session.status()
                    check_font(state)
                    require(state["host_feedback"]["chat"]["title"] == title,
                            "Synthetic title did not round-trip")
                    require(state["inputs_tx"] == before["inputs_tx"], "Native input was sent")
                    capture = session.screenshot(output.parent / f"tab5-title-sample-{index}-{VERSION}.png")
                    report["samples"].append({"title": title, "theme": theme.lower(),
                                               "synthetic_preview": True, "capture": capture})
            finally:
                session.recover_frame()
                session.command(f"THEME {original}", f"THEME {original}")
                if attempted:
                    session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                after = session.status()
                require(not after["input_capture"] and not after["lighting_preview"],
                        "Preview cleanup failed")
                require(after["inputs_tx"] == before["inputs_tx"], "Native input counter changed")
                report["after_samples"] = after

        # Release the serial lock between live captures so the production
        # companion can refresh its real metadata instead of expiring its TTL.
        for theme in ("LIGHT", "DARK"):
            for attempt in range(6):
                time.sleep(1.1)
                with open_serial(timeout=10) as connection:
                    session = Session(connection, 10)
                    state = session.status()
                    check_font(state)
                    if not state["host_feedback"]["chat"]["fresh"]:
                        continue
                    require(not state["input_capture"] and not state["lighting_preview"],
                            "Live capture encountered a synthetic preview")
                    session.command(f"THEME {theme}", f"THEME {theme}")
                    time.sleep(.5)
                    before_frame = session.status()
                    if not before_frame["host_feedback"]["chat"]["fresh"]:
                        continue
                    capture = session.screenshot(output.parent / f"tab5-font-{theme.lower()}-{VERSION}.png")
                    capture["synthetic_preview"] = False
                    after_frame = session.status()
                    require(before_frame["inputs_tx"] == after_frame["inputs_tx"],
                            "Native inputs changed during live capture")
                    require(not after_frame["input_capture"] and not after_frame["lighting_preview"],
                            "Live capture became a synthetic preview")
                    # SCREEN copies the framebuffer immediately. Transferring
                    # its 1.84 MB blocks the exclusive metadata serial channel
                    # long enough for the displayed live state to expire later.
                    # Keep both timestamps; the immutable frame is authenticated
                    # by the fresh normal STATUS immediately before SCREEN.
                    report["live_captures"].append({"theme": theme.lower(), "capture": capture,
                                                     "before": before_frame, "after_transfer": after_frame,
                                                     "frame_state": "fresh live state at SCREEN snapshot"})
                    break
            else:
                raise TimeoutError("Live chat metadata was not fresh for capture")
        report["success"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if "before" in report:
            time.sleep(2.2)  # Let the production companion renew after SCREEN.
            with open_serial(timeout=10) as connection:
                session = Session(connection, 10)
                original = report["before"]["theme"].upper()
                session.command(f"THEME {original}", f"THEME {original}")
                report["final"] = session.status()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / f"artifacts/font-qa-{VERSION}.json")
    args = parser.parse_args()
    result = verify(args.output.resolve())
    print(json.dumps({"success": result["success"], "samples": len(result["samples"]),
                      "live_captures": len(result["live_captures"])}, indent=2))


if __name__ == "__main__":
    main()
