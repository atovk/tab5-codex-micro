#!/usr/bin/env python3
"""Verify synthetic two-line titles and deterministic vertical scrolling.

This tool never sends TITLES or HOST_CONFIG. All six visible slot titles and
the explicitly tracked chat are replaced only inside an owned input-capture
preview. Four optional framebuffer captures test motion and dirty restoration.
No native host input or captured key/joystick event is expected.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from PIL import Image, ImageChops

from tab5_transport import open_serial
from verify_device import read_line
from verify_neon import Session, changed_pixels, require


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.10.2"
FIRMWARE = f"tab5-micro-{VERSION}"
ROW_HEIGHT = 25
VIEW_HEIGHT = 50
STEP_MS = 3200
SHORT_TITLE = "合成短题"
TWO_LINE_TITLE = "春夏秋冬天地玄黄宇宙洪荒"
LONG_TITLE = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜"
TRACKED_TITLE = "合成跟踪"
TITLES = (SHORT_TITLE, TWO_LINE_TITLE, LONG_TITLE, "㐀龘 é Œ", "emoji 😀 中", "Code é")
SCROLL_FIELDS = ("lines", "visible_lines", "width", "height", "line_height", "offset",
                 "max_offset", "elapsed_ms", "cycle_ms", "paused", "frames", "cached_bytes",
                 "ready", "manual")
INTEGER_FIELDS = tuple(field for field in SCROLL_FIELDS if field not in ("paused", "ready", "manual"))


class TitleSession(Session):
    def line(self, deadline: float) -> bytes:
        # A real title may contain the word ERROR. Never interpret JSON body
        # text as a protocol error or echo a complete private STATUS as one.
        while True:
            raw = read_line(self.connection, deadline)
            if raw.startswith(b"INPUT_EVENT "):
                self.events.append(json.loads(raw.removeprefix(b"INPUT_EVENT ")))
                continue
            if not raw.startswith(b"{") and (raw.endswith(b"_ERROR") or raw.startswith(b"ERROR")):
                raise RuntimeError("Device rejected a diagnostic command")
            return raw


def preview_fixtures(titles: tuple[str, ...] | list[str]) -> list[dict]:
    require(len(titles) == 6, "A title preview must cover every visible slot")
    require(all(isinstance(title, str) and len(title.encode("utf-8")) <= 191 for title in titles),
            "Synthetic title exceeds the preview byte limit")
    # Disable every light animation so framebuffer differences isolate titles.
    return [{"c": 0, "b": 0.0, "e": 0, "s": 0.0, "title": title} for title in titles]


def host_fixture(title: str = TRACKED_TITLE) -> dict:
    zone = {"c": 0, "b": 0.0, "e": 0, "s": 0.0, "m": 0}
    return {"rgb": {"keys": dict(zone), "ambient": dict(zone)},
            "config": {"v": 1, "seq": 1, "source": "local-config", "tier": "unknown",
                       "encoder": "unknown", "voice": "unknown", "actions": ["unknown"] * 7,
                       "chat": {"source": "desktop-ipc", "scope": "explicit-target",
                                "thread_id": "00000000-0000-0000-0000-000000000001",
                                "title": title, "speed": "unknown", "mode": "unknown",
                                "permission": "unknown"}},
            "feedback": [None] * 7}


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def scrolls(status: dict) -> list[dict]:
    value = status.get("title_scroll")
    require(isinstance(value, list) and len(value) == 7,
            "Expected seven title-scroll diagnostic entries")
    result = []
    for index, panel in enumerate(value):
        require(isinstance(panel, dict) and all(field in panel for field in SCROLL_FIELDS),
                f"Incomplete title-scroll metadata for panel {index}")
        require(all(type(panel[field]) is int and panel[field] >= 0 for field in INTEGER_FIELDS),
                f"Invalid numeric title-scroll metadata for panel {index}")
        require(all(type(panel[field]) is bool for field in ("paused", "ready", "manual")),
                f"Invalid title-scroll flags for panel {index}")
        result.append({field: panel[field] for field in SCROLL_FIELDS})
    return result


def safe_metadata(status: dict) -> dict:
    """Whitelist metadata; STATUS can also contain private real chat fields."""
    keys = ("firmware", "theme", "inputs_tx", "captured_inputs", "input_capture",
            "lighting_preview", "effects_frozen", "settings_open", "title_count", "titles_fresh",
            "render_last_us", "render_max_us", "input_interval_last_us", "input_interval_max_us")
    result = {key: status[key] for key in keys if key in status}
    font = status.get("title_font", {})
    result["title_font"] = {key: font[key] for key in ("loaded", "glyphs", "asset_bytes") if key in font}
    if "title_scroll" in status:
        result["title_scroll"] = scrolls(status)
    return result


def safety(status: dict, baseline: dict, session: Session, capture: bool = True) -> None:
    require(status["inputs_tx"] == baseline["inputs_tx"], "Native host input counter changed")
    require(status["captured_inputs"] == baseline["captured_inputs"], "Unexpected captured input")
    require(not session.events, "Unexpected captured key or joystick event")
    require(status["input_capture"] is capture, "Unexpected input-capture state")


def wait_status(session: Session, baseline: dict, predicate, message: str,
                timeout: float | None = None) -> dict:
    deadline = time.monotonic() + (session.timeout if timeout is None else min(timeout, session.timeout))
    while time.monotonic() < deadline:
        current = session.status()
        safety(current, baseline, session)
        if predicate(current):
            return current
        time.sleep(0.02)
    raise TimeoutError(message)


def reject(session: Session, command: str, expected: str) -> None:
    # Session.line intentionally raises on ERROR; expected rejection reads raw.
    session.send(command)
    deadline = time.monotonic() + session.timeout
    while time.monotonic() < deadline:
        raw = read_line(session.connection, deadline)
        if raw.startswith(b"INPUT_EVENT "):
            session.events.append(json.loads(raw.removeprefix(b"INPUT_EVENT ")))
            continue
        if raw == expected.encode():
            return
        require(not raw.endswith(b"_OK"), "Rejected diagnostic command was accepted")
        require(b"ERROR" not in raw, "Unexpected diagnostic error during rejection check")
    raise TimeoutError("No expected diagnostic rejection")


def apply_preview(session: Session, titles=TITLES, tracked: str = TRACKED_TITLE) -> None:
    session.command("PREVIEW_LIGHTS " + encode(preview_fixtures(titles)), "PREVIEW_LIGHTS_OK")
    # PREVIEW_LIGHTS starts from the real model. Override the tracked title too
    # before allowing any screenshot, even when only one slot title changed.
    session.command("PREVIEW_HOST " + encode(host_fixture(tracked)), "PREVIEW_HOST_OK")


def set_clock(session: Session, baseline: dict, index: int, now: int,
              offset: int | None = None, elapsed: int | None = None,
              paused: bool | None = False) -> dict:
    session.command(f"TITLE_SCROLL_TEST {index} {now}", "TITLE_SCROLL_TEST_OK")

    def matches(status):
        panel = scrolls(status)[index]
        return (panel["manual"] and panel["ready"]
                and (offset is None or panel["offset"] == offset)
                and (elapsed is None or panel["elapsed_ms"] == elapsed)
                and (paused is None or panel["paused"] is paused))

    return wait_status(session, baseline, matches, f"Title panel {index} did not reach its expected clock state")


def layout_contract(status: dict) -> list[dict]:
    panels = scrolls(status)
    for index, panel in enumerate(panels):
        require(panel["ready"] and panel["cached_bytes"] > 0, f"Panel {index} has no cached title")
        require(panel["height"] == VIEW_HEIGHT and panel["line_height"] == ROW_HEIGHT,
                f"Panel {index} has unexpected row/viewport geometry")
        require(panel["width"] == (182 if index == 6 else 162), f"Panel {index} has unexpected width")
        require(panel["visible_lines"] == min(panel["lines"], 2),
                f"Panel {index} has unexpected visible line count")
        expected_max = panel["lines"] * ROW_HEIGHT - 1 if panel["lines"] > 2 else 0
        require(panel["max_offset"] == expected_max and panel["offset"] <= expected_max,
                f"Panel {index} has an invalid scroll range")
        if panel["lines"] > 2:
            require(panel["cycle_ms"] == panel["lines"] * STEP_MS,
                    f"Panel {index} has an unexpected scroll cycle")
    require(panels[0]["lines"] == 1, "Short title should occupy exactly one line")
    require(panels[1]["lines"] == 2, "Full-width two-line fixture did not occupy exactly two lines")
    require(panels[2]["lines"] > 2, "Long title did not overflow the two-line viewport")
    require(panels[6]["lines"] == 1, "Tracked synthetic short title did not occupy one line")
    return panels


def compare_motion(first: Path, second: Path, output: Path, index: int = 2) -> dict:
    with Image.open(first) as a, Image.open(second) as b:
        difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    x, y = 28 + 206 * index + 16, 133
    box = (x, y, x + 162, y + VIEW_HEIGHT)
    inside = changed_pixels(difference.crop(box))
    outside = difference.copy()
    outside.paste((0, 0, 0), box)
    require(inside > 0, "Scroll metadata changed but the title pixels did not move")
    require(changed_pixels(outside) == 0, "Title motion changed pixels outside its viewport")
    difference.save(output)
    return {"changed_title_pixels": inside, "changed_outside_title_pixels": 0,
            "viewport": list(box), "difference_path": str(output.resolve())}


def compare_restoration(dirty: Path, full: Path, output: Path) -> dict:
    with Image.open(dirty) as a, Image.open(full) as b:
        difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    count = changed_pixels(difference)
    difference.save(output)
    require(count == 0, f"Dirty title replacement differs from a full rebuild at {count} pixels")
    return {"changed_pixels": count, "difference_path": str(output.resolve())}


def two_line_pixels(path: Path) -> dict:
    # The light-theme title viewport is inside the flat key face. Dark ink in
    # each independent 25px band confirms that the second row was rasterized.
    x, y = 28 + 206 + 16, 133
    with Image.open(path) as image:
        image = image.convert("RGB")
        rows = [sum(max(pixel) < 120 for pixel in image.crop(
            (x, y + row * ROW_HEIGHT, x + 162, y + (row + 1) * ROW_HEIGHT)).get_flattened_data())
                for row in range(2)]
    require(all(count > 20 for count in rows), "Two-line fixture has no visible ink in both rows")
    return {"dark_ink_pixels_by_row": rows, "row_height": ROW_HEIGHT}


def set_theme(session: Session, baseline: dict, theme: str) -> dict:
    old_frames = session.status().get("render_samples", 0)
    session.command("THEME " + theme.upper(), "THEME " + theme.upper())
    return wait_status(session, baseline,
                       lambda status: status["theme"] == theme and status.get("render_samples", 0) > old_frames,
                       "Theme redraw did not complete")


def verify(output: Path, port: str | None = None, timeout: float = 10,
           screenshots: bool = True) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"firmware": FIRMWARE, "synthetic_preview_only": True,
              "live_titles_modified": False, "host_config_modified": False,
              "checks": {}, "captures": [], "success": False}
    failure = None
    with open_serial(port, timeout) as connection:
        session = TitleSession(connection, timeout)
        before = session.status()
        require(before["firmware"] == FIRMWARE, f"Expected {FIRMWARE}")
        require(not before["input_capture"], "Another input-capture session is active")
        require(not before["lighting_preview"], "Another preview session is active")
        require(not before.get("settings_open", False), "Close settings before title QA")
        require(before["theme"] in ("light", "dark"), "Unknown starting theme")
        require(before.get("title_font", {}).get("loaded") is True, "Packed title font is unavailable")
        require(before["title_font"]["glyphs"] == 30445, "Unexpected title-font coverage")
        report["before"] = safe_metadata(before)
        capture_attempted = False
        try:
            reject(session, "TITLE_SCROLL_TEST 2 0", "TITLE_SCROLL_TEST_ERROR")
            reject(session, "PREVIEW_LIGHTS " + encode(preview_fixtures(TITLES)), "PREVIEW_LIGHTS_ERROR")
            current = session.status()
            safety(current, before, session, capture=False)
            require(not current["lighting_preview"], "Rejected outside-capture preview was activated")
            report["checks"]["outside_capture_rejected"] = True

            # BEGIN may take effect even if its acknowledgement is lost.
            capture_attempted = True
            session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
            session.command("MICRO", "MICRO")
            apply_preview(session)
            current = wait_status(session, before, lambda state: all(panel["ready"] for panel in scrolls(state))
                                  and scrolls(state)[0]["lines"] == 1
                                  and scrolls(state)[1]["lines"] == 2
                                  and scrolls(state)[2]["lines"] > 2
                                  and scrolls(state)[6]["lines"] == 1,
                                  "Synthetic title caches were not ready")
            initial = layout_contract(current)
            report["checks"]["layout"] = initial

            # Exercise the production wall-clock scheduler before using any
            # deterministic pose. No screenshot or diagnostic offset drives it.
            session.command("TITLE_SCROLL_TEST CLEAR", "TITLE_SCROLL_TEST_CLEARED")
            session.command("FX LIVE", "FX LIVE")
            session.command("PERF_RESET", "OK")
            auto_first = wait_status(session, before,
                                    lambda state: not scrolls(state)[2]["manual"]
                                    and scrolls(state)[2]["elapsed_ms"] > 2200
                                    and scrolls(state)[2]["offset"] > 0,
                                    "Natural title scrolling did not begin within six seconds", timeout=6)
            first_panel = scrolls(auto_first)[2]
            first_bytes = [panel["cached_bytes"] for panel in scrolls(auto_first)]
            time.sleep(0.12)
            auto_second = wait_status(session, before,
                                     lambda state: not scrolls(state)[2]["manual"]
                                     and scrolls(state)[2]["elapsed_ms"] > first_panel["elapsed_ms"]
                                     and scrolls(state)[2]["offset"] > first_panel["offset"]
                                     and scrolls(state)[2]["frames"] > first_panel["frames"],
                                     "Natural title scrolling did not continue", timeout=2)
            require([panel["cached_bytes"] for panel in scrolls(auto_second)] == first_bytes,
                    "Natural scrolling changed retained cache allocation")
            report["checks"]["natural_wall_clock"] = {
                "first": first_panel, "second": scrolls(auto_second)[2],
                "cached_bytes_stable": True, "captured_event_count": 0,
                "render_max_us": auto_second["render_max_us"],
                "input_interval_max_us": auto_second["input_interval_max_us"],
                "timing_scope": "Idle cached scrolling; repeated STATUS diagnostics also ran"}
            session.command("FX FREEZE", "FX FREEZE")
            wait_status(session, before,
                        lambda state: all(panel["offset"] == 0 and not panel["manual"]
                                          for panel in scrolls(state)),
                        "Freezing effects did not restore all title offsets to zero")

            for index in (0, 1, 6):
                set_clock(session, before, index, 0, offset=0)
                set_clock(session, before, index, 50000, offset=0)
            report["checks"]["one_and_two_lines_static"] = True

            # A real tracked-chat title uses the same policy without modifying
            # the explicitly selected desktop target or its saved configuration.
            apply_preview(session, tracked=LONG_TITLE)
            current = wait_status(session, before, lambda state: scrolls(state)[6]["lines"] > 2,
                                  "Tracked long title did not overflow")
            set_clock(session, before, 6, 0, offset=0, elapsed=0)
            set_clock(session, before, 6, 2700, offset=12, elapsed=2700)
            apply_preview(session)
            wait_status(session, before, lambda state: scrolls(state)[6]["lines"] == 1
                        and scrolls(state)[6]["offset"] == 0 and scrolls(state)[6]["elapsed_ms"] == 0,
                        "Tracked title replacement did not reset scrolling")
            report["checks"]["tracked_title_policy_and_reset"] = True

            cycle = initial[2]["cycle_ms"]
            maximum = initial[2]["max_offset"]
            steps = ((0, 0), (2199, 0), (2200, 0), (2700, 12), (3199, 24),
                     (3200, 25), (cycle - 1, maximum), (cycle, 0))
            clock_results = []
            first_frames = None
            for now, offset in steps:
                current = set_clock(session, before, 2, now, offset=offset, elapsed=now)
                panel = scrolls(current)[2]
                first_frames = panel["frames"] if first_frames is None else first_frames
                clock_results.append({"now_ms": now, **panel})
            require(clock_results[-1]["frames"] > first_frames,
                    "Moving clock states did not produce any title frames")
            report["checks"]["dwell_motion_wrap"] = clock_results

            # No key lies at this blank bottom margin, so touch pause must not
            # produce even a captured native input event.
            set_clock(session, before, 2, 0, offset=0, elapsed=0)
            set_clock(session, before, 2, 2700, offset=12, elapsed=2700)
            session.touch(640, 704, True)
            wait_status(session, before, lambda state: scrolls(state)[2]["paused"],
                        "Touch did not pause title scrolling")
            held = set_clock(session, before, 2, 4700, offset=12, elapsed=2700, paused=True)
            session.touch(640, 704, False)
            wait_status(session, before, lambda state: not scrolls(state)[2]["paused"],
                        "Release did not leave the touch-paused state")
            set_clock(session, before, 2, 5200, offset=12, elapsed=2700)
            set_clock(session, before, 2, 5700, offset=12, elapsed=2700)
            resumed = set_clock(session, before, 2, 6200, offset=25, elapsed=3200)
            report["checks"]["touch_pause_and_resume_hold"] = {
                "held": scrolls(held)[2], "resumed": scrolls(resumed)[2],
                "captured_event_count": 0, "resume_hold_ms": 1000}

            # Freeze a valid baseline before all malformed manual-clock commands.
            stable = scrolls(session.status())
            for command in ("TITLE_SCROLL_TEST -1 0", "TITLE_SCROLL_TEST 7 0",
                            "TITLE_SCROLL_TEST 2 -1", "TITLE_SCROLL_TEST 2 600001",
                            "TITLE_SCROLL_TEST 2 100 extra"):
                reject(session, command, "TITLE_SCROLL_TEST_ERROR")
                current = session.status()
                safety(current, before, session)
                require(scrolls(current) == stable, "Rejected clock command changed title metadata")
            report["checks"]["invalid_clock_atomic"] = True

            if screenshots:
                if session.status()["theme"] != "light":
                    set_theme(session, before, "light")
                set_clock(session, before, 2, 0, offset=0, elapsed=0)
                top = output.parent / f"tab5-title-top-{VERSION}.png"
                report["captures"].append(session.screenshot(top))
                require(scrolls(session.status())[2]["offset"] == 0, "Framebuffer transfer advanced the frozen clock")
                report["checks"]["two_line_pixels"] = two_line_pixels(top)

                set_clock(session, before, 2, 2700, offset=12, elapsed=2700)
                middle = output.parent / f"tab5-title-middle-{VERSION}.png"
                report["captures"].append(session.screenshot(middle))
                require(scrolls(session.status())[2]["offset"] == 12,
                        "Framebuffer transfer advanced the frozen middle state")
                report["checks"]["motion_pixels"] = compare_motion(
                    top, middle, output.parent / f"tab5-title-motion-diff-{VERSION}.png")

                replacement = list(TITLES)
                replacement[2] = SHORT_TITLE
                apply_preview(session, replacement)
                reset = wait_status(session, before, lambda state: scrolls(state)[2]["ready"]
                                    and scrolls(state)[2]["lines"] == 1
                                    and scrolls(state)[2]["offset"] == 0
                                    and scrolls(state)[2]["elapsed_ms"] == 0,
                                    "Overflow-to-short replacement did not reset scrolling")
                dirty = output.parent / f"tab5-title-short-dirty-{VERSION}.png"
                report["captures"].append(session.screenshot(dirty))
                # Theme round-trip forces a full canvas reconstruction while
                # preserving the same synthetic state and frozen manual time.
                set_theme(session, before, "dark")
                set_theme(session, before, "light")
                full = output.parent / f"tab5-title-short-full-{VERSION}.png"
                set_clock(session, before, 2, 2700, offset=0, elapsed=0)
                report["captures"].append(session.screenshot(full))
                report["checks"]["dirty_restoration"] = compare_restoration(
                    dirty, full, output.parent / f"tab5-title-restoration-diff-{VERSION}.png")
                report["checks"]["overflow_to_short_reset"] = scrolls(reset)[2]
                report["checks"]["pixel_scope"] = "Synthetic light theme; motion viewport and full-frame restoration"
            else:
                replacement = list(TITLES)
                replacement[2] = SHORT_TITLE
                apply_preview(session, replacement)
                reset = wait_status(session, before, lambda state: scrolls(state)[2]["lines"] == 1
                                    and scrolls(state)[2]["offset"] == 0
                                    and scrolls(state)[2]["elapsed_ms"] == 0,
                                    "Overflow-to-short replacement did not reset scrolling")
                report["checks"]["overflow_to_short_reset"] = scrolls(reset)[2]
                report["checks"]["pixel_scope"] = "Skipped by --no-screenshots"
        except Exception as error:
            failure = error
            report["error"] = f"{type(error).__name__}: {error}"
        finally:
            cleanup_errors = []
            stream_ready = True
            try:
                session.recover_frame()
            except Exception as error:
                stream_ready = False
                cleanup_errors.append(f"Framebuffer recovery: {type(error).__name__}: {error}")
            if stream_ready and capture_attempted:
                try:
                    current = session.status()
                    if current["input_capture"]:
                        session.touch(640, 704, False)
                        session.command("TITLE_SCROLL_TEST CLEAR", "TITLE_SCROLL_TEST_CLEARED")
                        session.command("THEME " + before["theme"].upper(), "THEME " + before["theme"].upper())
                        session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                except Exception as error:
                    cleanup_errors.append(f"End owned capture: {type(error).__name__}: {error}")
            if stream_ready:
                try:
                    after = session.status()
                    safety(after, before, session, capture=False)
                    require(not after["lighting_preview"], "Preview survived capture cleanup")
                    require(after["effects_frozen"] == before["effects_frozen"], "Effects preference was not restored")
                    require(after["theme"] == before["theme"], "Theme was not restored")
                    require(not any(panel["manual"] for panel in scrolls(after)), "Manual title clock survived cleanup")
                    require(after["title_count"] == before["title_count"], "Live title count changed during preview QA")
                    report["after"] = safe_metadata(after)
                except Exception as error:
                    cleanup_errors.append(f"Cleanup verification: {type(error).__name__}: {error}")
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                if failure is None:
                    failure = RuntimeError("Title-scroll QA cleanup failed")
            report["captured_event_count"] = len(session.events)
            report["inputs_tx_unchanged"] = "after" in report and report["after"]["inputs_tx"] == before["inputs_tx"]
            report["success"] = failure is None
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failure is not None:
        raise RuntimeError(f"Title-scroll QA failed; metadata report: {output}") from failure
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/qa/title-scroll-0.10.2.json")
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--no-screenshots", action="store_true")
    args = parser.parse_args()
    result = verify(args.output, args.port, args.timeout, not args.no_screenshots)
    print(json.dumps({"success": result["success"], "synthetic_preview_only": True,
                      "captured_event_count": result["captured_event_count"],
                      "inputs_tx_unchanged": result["inputs_tx_unchanged"],
                      "report": str(args.output.expanduser().resolve()),
                      "captures": [item["path"] for item in result["captures"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
