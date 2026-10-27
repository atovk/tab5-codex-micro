#!/usr/bin/env python3
"""Capture both on-device themes and verify selection survives a restart.

Only local screen/theme commands are used. Native Micro inputs are never sent.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from PIL import Image, ImageStat

from tab5_transport import open_serial
from verify_device import read_line, screenshot, status


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
VERSION = "0.11.0"

# Pillow boxes exclude the right/bottom edges. These patches stay completely
# inside flat faces, away from AA, bevels, dial marks/text and the stick axes.
SURFACE_PATCHES = {
    "body": (600, 8, 650, 24),
    "tray": (52, 280, 112, 450),
    "key_face": (60, 554, 146, 566),
    # Dial center (244,352): r63.5..71.1 clears its r60 inset, r78 dot and ticks.
    "dial_face": (173, 342, 181, 362),
    "dial_center_face": (230, 316, 258, 326),
    # Stick center (802,352): r62.9..81.3 clears the r62 ring and both axes.
    "stick_recess": (744, 294, 758, 308),
    "knob_center": (790, 339, 800, 347),
}

# Current Micro UI face colors before nearest-565 conversion. The dial is the
# 50% RGB888 mix of metalTop/metalBottom; center and knob use the idle keyTop.
CONTROL_FACE_RGB = {
    "dark": {"dial_face": 0x647186, "dial_center_face": 0x37414D,
             "stick_recess": 0x151C27, "knob_center": 0x37414D},
    "light": {"dial_face": 0xD9E0E9, "dial_center_face": 0xFFFFFF,
              "stick_recess": 0xC6CFDC, "knob_center": 0xFFFFFF},
}


def surface_rgb(rgb888: int) -> tuple[int, int, int]:
    """Match surface565() and the screenshot's RGB565 bit replication."""
    red = (((rgb888 >> 16) & 255) * 31 + 127) // 255
    green = (((rgb888 >> 8) & 255) * 63 + 127) // 255
    blue = ((rgb888 & 255) * 31 + 127) // 255
    return ((red << 3) | (red >> 2), (green << 2) | (green >> 4),
            (blue << 3) | (blue >> 2))


def inspect_uniform_surfaces(theme: str, frame: Image.Image) -> dict:
    """Reject any grain in the broad faces, including a single changed pixel."""
    assert frame.size == (1280, 720), frame.size
    surfaces = {}
    for name, box in SURFACE_PATCHES.items():
        colors = frame.crop(box).getcolors(maxcolors=20000)
        assert colors and len(colors) == 1, (theme, name, len(colors) if colors else None)
        rgb = colors[0][1]
        entry = {"box": box, "unique_colors": len(colors), "rgb": rgb}
        if name in CONTROL_FACE_RGB[theme]:
            expected = surface_rgb(CONTROL_FACE_RGB[theme][name])
            assert rgb == expected, (theme, name, rgb, expected)
            entry["expected_rgb"] = expected
        surfaces[name] = entry
    return surfaces


def command(value: str, expected: str | None = None) -> None:
    with open_serial(timeout=10) as connection:
        connection.write((value + "\n").encode())
        connection.flush()
        if expected is None:
            return
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            line = read_line(connection, deadline).decode("utf-8", errors="replace")
            if line == expected:
                return
            if "ERROR" in line:
                raise RuntimeError(line)
    raise TimeoutError(f"No acknowledgement for {value}")


def restart_and_check(theme: str) -> dict:
    # Let the UI render and persist its pending preference before rebooting.
    time.sleep(.8)
    command("RESTART")
    time.sleep(3)
    deadline = time.monotonic() + 18
    last_error = None
    while time.monotonic() < deadline:
        try:
            current = status(None, 3)["status"]
            assert current["theme"] == theme, current
            assert not current["input_capture"], current
            assert current["inputs_tx"] == 0, current
            return current
        except (OSError, RuntimeError, TimeoutError) as error:
            last_error = error
            time.sleep(.5)
    raise TimeoutError(f"Device did not reconnect: {last_error}")


def game_tap(x: int, y: int) -> None:
    command(f"GAME_TOUCH {x} {y} 1", "OK")
    command(f"GAME_TOUCH {x} {y} 0", "OK")
    time.sleep(.15)


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    before = status(None, 10)["status"]
    assert before["firmware"] == f"tab5-micro-{VERSION}", before
    assert not before["input_capture"], before
    captures = []
    persisted = {}
    for theme in ("dark", "light"):
        command(f"THEME {theme.upper()}", f"THEME {theme.upper()}")
        persisted[theme] = restart_and_check(theme)
        command("MICRO", "MICRO")
        time.sleep(.5)
        captures.append(screenshot(None, 30, ARTIFACTS / f"tab5-{theme}-{VERSION}.png"))
        command("PLAY", "PLAY")
        time.sleep(.5)
        captures.append(screenshot(None, 30, ARTIFACTS / f"tab5-games-{theme}-{VERSION}.png"))
        for index, (name, x) in enumerate((("plane", 226), ("tetris", 640), ("dino", 1054)), 1):
            game_tap(x, 400)
            current = status(None, 5)["status"]
            assert current["game"] == index and current["game_state"] == 0, current
            game_tap(1040, 133)
            current = status(None, 5)["status"]
            assert current["game_state"] == 1, current
            captures.append(screenshot(None, 30, ARTIFACTS / f"tab5-{name}-{theme}-{VERSION}.png"))
            game_tap(97, 133)
            assert status(None, 5)["status"]["game"] == 0
        command("MICRO", "MICRO")
    after = status(None, 10)["status"]
    assert after["theme"] == "light", after
    assert after["inputs_tx"] == 0, after
    luminance = {}
    for capture in captures:
        with Image.open(capture["path"]) as frame:
            luminance[Path(capture["path"]).stem] = ImageStat.Stat(frame.convert("L")).mean[0]
    # These patches contain only broad material faces, without text, icons,
    # shadows or bevels. Each should now be one exact 565 color, not a texture.
    uniform_surfaces = {}
    for theme in ("dark", "light"):
        with Image.open(ARTIFACTS / f"tab5-{theme}-{VERSION}.png") as frame:
            uniform_surfaces[theme] = inspect_uniform_surfaces(theme, frame)
    for page in ("tab5", "tab5-games"):
        difference = luminance[f"{page}-light-{VERSION}"] - luminance[f"{page}-dark-{VERSION}"]
        assert difference > 60, (page, luminance)
    result = {"firmware": after["firmware"], "restart_persistence": persisted,
              "captures": captures, "frame_mean_luminance": luminance,
              "uniform_surface_patches":uniform_surfaces, "final_status": after}
    output = ARTIFACTS / f"themes-qa-{VERSION}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"path": str(output), "themes_persisted": list(persisted),
                      "captures": [item["path"] for item in captures],
                      "host_inputs": after["inputs_tx"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
