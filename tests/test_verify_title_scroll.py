"""Exercise title framebuffer assertions without hardware or desktop input."""
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verify_title_scroll as qa


class TitleScrollPixelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def frame(self, name, offset=0, vertical=0, second_row=False):
        x0, y0, x1, y1 = qa.LAYOUT.title_box
        width = x1 - x0
        raster = Image.new("RGB", (width + 100, 50), (230, 230, 230))
        draw = ImageDraw.Draw(raster)
        for index, x in enumerate(range(5, width + 80, 17)):
            draw.rectangle((x, 15 + vertical, x + 4 + index % 7, 34 + vertical), fill=(50, 50, 50))
        if second_row:
            draw.rectangle((20, 1, 30, 10), fill=(50, 50, 50))
        frame = Image.new("RGB", (1280, 720), (230, 230, 230))
        frame.paste(raster.crop((offset, 0, offset + width, 50)), (x0, y0))
        path = self.root / name
        frame.save(path)
        return path

    def test_horizontal_pixels_move_twenty_pixels_with_one_ink_row(self):
        result = qa.compare_horizontal_motion(self.frame("first.png"),
                                              self.frame("moved.png", offset=20),
                                              self.root / "diff.png")
        self.assertEqual(result["horizontal_offset_pixels"], 20)
        self.assertEqual(result["aligned_ink_mismatch_pixels"], 0)
        self.assertEqual(result["changed_outside_title_pixels"], 0)

    def test_wrong_axis_or_displacement_is_rejected(self):
        first = self.frame("first.png")
        for offset, vertical in ((0, 2), (19, 0), (20, 2)):
            with self.subTest(offset=offset, vertical=vertical), self.assertRaises(RuntimeError):
                qa.compare_horizontal_motion(first, self.frame("bad.png", offset, vertical),
                                             self.root / "diff.png")

    def test_two_rows_cannot_pass_single_line_pixel_assertion(self):
        with self.assertRaisesRegex(RuntimeError, "multiple rows"):
            qa.single_line_pixels(self.frame("two.png", second_row=True))

    def test_dirty_restoration_rejects_residual_glyph(self):
        dirty = self.frame("dirty.png")
        full = self.frame("full.png")
        self.assertEqual(qa.compare_restoration(dirty, full, self.root / "diff.png")["changed_pixels"], 0)
        with Image.open(dirty) as image:
            image.putpixel((qa.LAYOUT.title_box[0] + 100, qa.LAYOUT.title_box[1] + 2), (50, 50, 50))
            image.save(dirty)
        with self.assertRaisesRegex(RuntimeError, "1 pixels"):
            qa.compare_restoration(dirty, full, self.root / "diff.png")

    def test_central_contract_distinguishes_horizontal_from_old_vertical_policy(self):
        panel = {"ready": True, "cached_bytes": 100, "width": 348, "height": 50,
                 "line_height": 25, "lines": 1, "visible_lines": 1, "max_offset": 120,
                 "offset": 20, "cycle_ms": 10400}
        qa.central_contract(panel, overflow=True)
        qa.central_contract({**panel, "max_offset": 0, "offset": 0, "cycle_ms": 0}, overflow=False)
        for changes in ({"lines": 3, "visible_lines": 2}, {"cycle_ms": 9600},
                        {"offset": 121}, {"width": 256}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                qa.central_contract({**panel, **changes}, overflow=True)


if __name__ == "__main__":
    unittest.main()
