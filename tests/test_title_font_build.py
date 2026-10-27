"""Pure standard-library coverage for the title font container and cmap reader."""
from pathlib import Path
import struct
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_title_font as font


def sfnt(cmap, glyph_count=32):
    tables = [(b"cmap", cmap), (b"maxp", struct.pack(">IH", 0x00005000, glyph_count))]
    offset = 12 + 16 * len(tables)
    records, payload = [], []
    for tag, data in tables:
        records.append(struct.pack(">4sIII", tag, 0, offset, len(data)))
        payload.append(data)
        offset += len(data)
    return struct.pack(">4sHHHH", b"OTTO", len(tables), 0, 0, 0) + b"".join(records + payload)


def cmap_fixture():
    # Format 4 covers A/B using delta and 龘 using a glyphIdArray + delta.
    f4 = (struct.pack(">7H", 4, 42, 0, 6, 4, 1, 2)
          + struct.pack(">3H", 0x42, 0x9F98, 0xFFFF) + b"\0\0"
          + struct.pack(">3H", 0x41, 0x9F98, 0xFFFF)
          + struct.pack(">3h", -64, 1, 1) + struct.pack(">3H", 0, 4, 0)
          + struct.pack(">H", 6))
    groups = [(0x20, 0x20, 1), (0x85, 0x85, 2), (0xE9, 0xE9, 3),
              (0x3400, 0x3400, 4), (0x10000, 0x10000, 5)]
    f12 = struct.pack(">HHIII", 12, 0, 16 + 12 * len(groups), 0, len(groups))
    f12 += b"".join(struct.pack(">III", *group) for group in groups)
    # A Unicode alias shares f4; a symbol-only entry must be ignored.
    return (struct.pack(">HH", 0, 4)
            + struct.pack(">HHI", 3, 1, 36)
            + struct.pack(">HHI", 3, 10, 36 + len(f4))
            + struct.pack(">HHI", 0, 3, 36)
            + struct.pack(">HHI", 3, 0, 0xFFFFFFFE)
            + f4 + f12)


def raw_fixture():
    # An odd glyph length forces a byte to contain the end of one glyph and
    # the beginning of the next; metrics must not introduce nibble alignment.
    header = struct.pack(">6I", 2, 11, 24, 0, 20, 4)
    metrics = struct.pack(">7i", 0x20, 1, 1, 6, 0, 0, 1)
    metrics += struct.pack(">7i", 0x41, 2, 2, 15, 19, -1, 0)
    return header + metrics + bytes((0, 255, 8, 9, 128))


def recalc(data):
    data = bytearray(data)
    struct.pack_into("<I", data, 28, zlib.crc32(data[64:]))
    return bytes(data)


class TitleFontBuildTests(unittest.TestCase):
    def test_all_coverage_levels_round_to_nearest_and_stay_bounded(self):
        raw = bytes(range(256))
        restored = font.unpack_pixels(font.pack_pixels(raw), len(raw))
        self.assertEqual(restored, bytes(((value + 8) // 17) * 17 for value in raw))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(raw, restored)), 8)
        self.assertEqual(restored[0], 0)
        self.assertEqual(restored[-1], 255)
        self.assertEqual(font.pack_pixels(b"\xFF\0\x88"), b"\xF0\x80")

    def test_nibbles_remain_continuous_across_odd_glyph_boundaries(self):
        raw = raw_fixture()
        asset = font.pack_vlw(raw)
        info = font.inspect_asset(asset)
        self.assertEqual(info["bitmap_start"], 80)
        self.assertEqual(asset[64:144], raw[:80])
        self.assertEqual(asset[144:], b"\x0F\x01\x80")
        self.assertEqual(font.unpack_vlw(asset), raw[:80] + bytes((0, 255, 0, 17, 136)))
        self.assertEqual(asset[:8], b"T5VLW4\0\0")
        self.assertEqual(struct.unpack_from("<6I", asset, 8),
                         (1, len(asset), len(raw), 80, 2, zlib.crc32(asset[64:])))
        self.assertFalse(any(asset[32:64]))

    def test_quantized_ink_retrims_bearings_and_keeps_blank_glyph_advances(self):
        raw = struct.pack(">6I", 2, 11, 24, 0, 20, 4)
        raw += struct.pack(">7i", 0x41, 4, 4, 18, 20, -2, 0)
        raw += struct.pack(">7i", 0x42, 1, 3, 17, 19, -1, 0)
        raw += bytes((8, 7, 6, 5, 4, 255, 128, 3, 2, 9, 17, 1, 8, 7, 6, 5))
        raw += bytes((1, 8, 1))
        trimmed = font.quantize_trim_vlw(raw)
        self.assertEqual(struct.unpack_from(">6I", trimmed), (2, 11, 24, 0, 19, 1))
        self.assertEqual(struct.unpack_from(">7i", trimmed, 24), (0x41, 2, 2, 18, 19, -1, 0))
        self.assertEqual(struct.unpack_from(">7i", trimmed, 52), (0x42, 1, 1, 17, 0, 0, 1))
        self.assertEqual(trimmed[80:], bytes((255, 136, 17, 17, 0)))
        self.assertEqual(font.unpack_vlw(font.pack_vlw(trimmed)), trimmed)
        self.assertEqual(font.quantize_trim_vlw(trimmed), trimmed)

    def test_header_crc_sizes_and_metadata_must_all_agree(self):
        original = font.pack_vlw(raw_fixture())
        cases = [original[:63], b"badmagic" + original[8:], original[:-1], original + b"\0"]
        for position, value in ((8, 2), (12, len(original) + 1), (16, 1), (20, 79), (24, 3), (32, 1)):
            changed = bytearray(original)
            struct.pack_into("<I", changed, position, value)
            cases.append(bytes(changed))
        changed = bytearray(original)
        changed[-1] ^= 0x10
        cases.append(bytes(changed))
        for data in cases:
            with self.subTest(data=data[:32]), self.assertRaises(ValueError):
                font.inspect_asset(data)
        changed = bytearray(original)
        changed[-1] |= 1
        with self.assertRaisesRegex(ValueError, "nibble"):
            font.inspect_asset(recalc(changed))
        changed = bytearray(original)
        struct.pack_into(">i", changed, 64 + 24 + 28, 0x20)  # Duplicate glyph code.
        with self.assertRaisesRegex(ValueError, "codepoint"):
            font.inspect_asset(recalc(changed))
        changed = bytearray(original)
        struct.pack_into(">i", changed, 64 + 24 + 4, 0)  # Zero bitmap height.
        with self.assertRaisesRegex(ValueError, "dimensions"):
            font.inspect_asset(recalc(changed))

    def test_empty_odd_and_invalid_pixel_lengths(self):
        self.assertEqual(font.pack_pixels(b""), b"")
        self.assertEqual(font.unpack_pixels(b"", 0), b"")
        for data, count in ((b"", 1), (b"\0", -1), (b"\xF1", 1), (b"\0\0", 2)):
            with self.subTest(data=data, count=count), self.assertRaises(ValueError):
                font.unpack_pixels(data, count)

    def test_unicode_cmap4_and12_union_with_symbol_controls_and_astral_excluded(self):
        self.assertEqual(font.supported_bmp_codes(sfnt(cmap_fixture())),
                         (0x20, 0x41, 0x42, 0xE9, 0x3400, 0x9F98))

    def test_corrupt_font_offsets_ranges_and_glyph_ids_fail_closed(self):
        valid = sfnt(cmap_fixture())
        cases = [b"", valid[:20], b"ttcf" + valid[4:]]
        changed = bytearray(valid)
        struct.pack_into(">I", changed, 20, len(valid) + 100)  # cmap table beyond file.
        cases.append(bytes(changed))
        changed = bytearray(cmap_fixture())
        struct.pack_into(">I", changed, 8, len(changed) + 2)  # Unicode subtable beyond cmap.
        cases.append(sfnt(changed))
        changed = bytearray(cmap_fixture())
        struct.pack_into(">H", changed, 36 + 38, 0xFFFE)  # Bad glyphIdArray range offset.
        cases.append(sfnt(changed))
        changed = bytearray(cmap_fixture())
        struct.pack_into(">H", changed, 36 + 36, 2)  # Points into another range-offset word.
        cases.append(sfnt(changed))
        cases.append(sfnt(cmap_fixture(), glyph_count=2))
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                font.supported_bmp_codes(data)

    def test_pinned_font_covers_rare_cjk_and_latin_without_personal_title_literals(self):
        path = ROOT / "artifacts/font-cache/NotoSansSC-Medium.otf"
        if not path.is_file():
            self.skipTest("Pinned upstream font is not cached; tests never download it")
        codes = font.supported_bmp_codes(path.read_bytes())
        self.assertEqual(len(codes), 30445)
        self.assertEqual(tuple(sorted(set(codes))), codes)
        for code in (0x20, 0x41, 0xE9, 0x152, 0x3400, 0x4E2D, 0x9F98):
            self.assertIn(code, codes)
        self.assertTrue(all(0x20 <= code <= 0xFFFF and not 0x7F <= code <= 0x9F
                            and not 0xD800 <= code <= 0xDFFF for code in codes))


if __name__ == "__main__":
    unittest.main()
