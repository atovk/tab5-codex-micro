from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import control_layout as layout
import verify_audio_performance as audio
import verify_controls as controls
import verify_title_scroll as titles
import verify_ui_pixels as pixels
import verify_theme as theme


class HardwareQALayoutTests(unittest.TestCase):
    def test_current_layout_matches_firmware_paint_and_touch_contract(self):
        layout.check_cpp_layout()
        self.assertEqual(layout.CURRENT.dial_x + layout.CURRENT.stick_x, 1280)
        self.assertEqual(layout.CURRENT.chat_x + layout.CURRENT.chat_width // 2, 640)
        self.assertLessEqual(layout.CURRENT.stick_arrow_radius + 8, layout.CURRENT.stick_radius)

    def test_cpp_layout_rejects_unknown_names_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.h"
            for expression in ("MissingRadius", "get_radius()", "100 / 0"):
                with self.subTest(expression=expression):
                    path.write_text(f"constexpr int StickRadius = {expression};\n")
                    with self.assertRaises(ValueError):
                        layout.check_cpp_layout(path)

    def test_cpp_layout_rejects_drift_in_derived_coordinates(self):
        source = (ROOT / "include/control_layout.h").read_text()
        self.assertIn("TrayWidth = 396", source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.h"
            for old, new in (("TrayWidth = 396", "TrayWidth = 392"),
                             ("StickArrowRadius = 92", "StickArrowRadius = 105")):
                with self.subTest(constant=old):
                    self.assertIn(old, source)
                    path.write_text(source.replace(old, new))
                    with self.assertRaisesRegex(ValueError, "coordinates differ"):
                        layout.check_cpp_layout(path)

    def test_exact_current_and_legacy_versions_select_distinct_coordinates(self):
        for value in ("0.13.1", "tab5-micro-0.13.1"):
            self.assertIs(layout.for_firmware(value), layout.CURRENT)
        for version in ("0.11.1", "0.12.0", "0.13.0"):
            for value in (version, "tab5-micro-" + version):
                self.assertIs(layout.for_firmware(value), layout.LEGACY)
        self.assertNotEqual(layout.CURRENT.stick_x, layout.LEGACY.stick_x)
        self.assertEqual(layout.LEGACY.stick_arrow_radius, 105)

    def test_unverified_layout_never_opens_hardware(self):
        with patch.object(controls, "open_serial") as serial:
            for version in ("0.13.10", "0.13.1-rc1", "0.14.0", "*", "0.13.1 extra"):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    controls.verify(Path("unused.json"), firmware=version)
            serial.assert_not_called()

    def test_fixed_current_qa_tools_pin_the_current_firmware(self):
        self.assertEqual(audio.FIRMWARE, layout.FIRMWARE)
        self.assertEqual(pixels.FIRMWARE, layout.FIRMWARE)
        self.assertEqual(titles.FIRMWARE, layout.FIRMWARE)
        self.assertEqual(theme.VERSION, layout.VERSION)

    def test_audio_safety_does_not_inherit_legacy_verifier_pin(self):
        current = {"firmware": layout.FIRMWARE, "inputs_tx": 1,
                   "input_capture": True, "captured_inputs": 0}
        session = SimpleNamespace(events=[])
        audio.check_safety(session, current, current)
        with self.assertRaises(RuntimeError):
            audio.check_safety(session, {**current, "firmware": "tab5-micro-0.13.0"}, current)

    def test_tracked_title_motion_uses_centered_viewport(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            a, b, diff = (directory / name for name in ("a.png", "b.png", "diff.png"))
            first = Image.new("RGB", (1280, 720))
            first.save(a)
            second = first.copy()
            second.putpixel((layout.CURRENT.title_box[0], layout.CURRENT.title_box[1]), (255, 255, 255))
            second.save(b)
            result = titles.compare_motion(a, b, diff, index=6)
            self.assertEqual(result["viewport"], list(layout.CURRENT.title_box))
            self.assertEqual(result["changed_title_pixels"], 1)
            second.putpixel((1107, 401), (255, 255, 255))
            second.save(b)
            with self.assertRaises(RuntimeError):
                titles.compare_motion(a, b, diff, index=6)


if __name__ == "__main__":
    unittest.main()
