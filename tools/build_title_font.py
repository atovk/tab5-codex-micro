#!/usr/bin/env python3
"""Build one full-BMP Noto title face as a seekable, packed 4-bit VLW asset.

Only the bitmap stream is packed. VLW header and metric bytes retain their
original virtual offsets, so firmware can seek without a decompressed copy.
Pillow is needed only for generation, using the existing UI font generator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import unicodedata
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts/tab5-title-font-v1.bin"
MAGIC = b"T5VLW4\0\0"
HEADER_SIZE = 64
MAX_ASSET_SIZE = 0xAE0000
FONT_SIZE = 24


def _range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) - size:
        raise ValueError(f"Truncated {label}")


def _tables(data: bytes) -> dict[bytes, bytes]:
    _range(data, 0, 12, "sfnt header")
    if data[:4] not in (b"OTTO", b"\0\1\0\0", b"true"):
        raise ValueError("Unsupported sfnt signature")
    count = struct.unpack_from(">H", data, 4)[0]
    if not count:
        raise ValueError("Empty sfnt directory")
    _range(data, 12, count * 16, "sfnt directory")
    result = {}
    for index in range(count):
        tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + index * 16)
        _range(data, offset, length, "sfnt table")
        if tag in result:
            raise ValueError("Duplicate sfnt table")
        result[tag] = data[offset:offset + length]
    return result


def _format4(data: bytes, glyph_count: int) -> set[int]:
    _range(data, 0, 16, "format 4 header")
    length = struct.unpack_from(">H", data, 2)[0]
    doubled = struct.unpack_from(">H", data, 6)[0]
    if not doubled or doubled % 2 or length > len(data):
        raise ValueError("Invalid format 4 length/segments")
    data = data[:length]
    count = doubled // 2
    _range(data, 0, 16 + count * 8, "format 4 segments")
    end_at, start_at, delta_at, range_at = 14, 16 + count * 2, 16 + count * 4, 16 + count * 6
    result = set()
    previous = -1
    for index in range(count):
        end = struct.unpack_from(">H", data, end_at + index * 2)[0]
        start = struct.unpack_from(">H", data, start_at + index * 2)[0]
        delta = struct.unpack_from(">h", data, delta_at + index * 2)[0]
        offset = struct.unpack_from(">H", data, range_at + index * 2)[0]
        if start > end or start <= previous or offset % 2:
            raise ValueError("Invalid format 4 segment ordering/offset")
        previous = end
        for code in range(start, end + 1):
            if offset:
                word_at = range_at + index * 2 + offset + (code - start) * 2
                if word_at < 16 + count * 8:
                    raise ValueError("Format 4 glyph array points inside metadata")
                _range(data, word_at, 2, "format 4 glyph array")
                glyph = struct.unpack_from(">H", data, word_at)[0]
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            else:
                glyph = (code + delta) & 0xFFFF
            if glyph >= glyph_count:
                raise ValueError("Format 4 glyph exceeds maxp")
            if glyph and code != 0xFFFF:
                result.add(code)
    return result


def _format12(data: bytes, glyph_count: int) -> set[int]:
    _range(data, 0, 16, "format 12 header")
    _, reserved, length, _, count = struct.unpack_from(">HHIII", data)
    if reserved or length > len(data) or length != 16 + count * 12:
        raise ValueError("Invalid format 12 length/groups")
    result = set()
    previous = -1
    for index in range(count):
        start, end, glyph = struct.unpack_from(">III", data, 16 + index * 12)
        if start > end or start <= previous or end > 0x10FFFF:
            raise ValueError("Invalid format 12 group ordering")
        if glyph + end - start >= glyph_count:
            raise ValueError("Format 12 glyph exceeds maxp")
        previous = end
        for code in range(start, min(end, 0xFFFF) + 1):
            if glyph + code - start:
                result.add(code)
    return result


def supported_bmp_codes(data: bytes) -> tuple[int, ...]:
    """Union Unicode cmap 4/12 mappings, excluding controls and surrogates."""
    tables = _tables(data)
    if b"cmap" not in tables or b"maxp" not in tables:
        raise ValueError("Font requires cmap and maxp")
    maxp = tables[b"maxp"]
    _range(maxp, 0, 6, "maxp")
    glyph_count = struct.unpack_from(">H", maxp, 4)[0]
    if not glyph_count:
        raise ValueError("Empty maxp glyph count")
    cmap = tables[b"cmap"]
    _range(cmap, 0, 4, "cmap header")
    version, count = struct.unpack_from(">HH", cmap)
    if version:
        raise ValueError("Unsupported cmap version")
    _range(cmap, 4, count * 8, "cmap records")
    codes, visited = set(), set()
    for index in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", cmap, 4 + index * 8)
        if not (platform == 0 or (platform == 3 and encoding in (1, 10))):
            continue
        if offset in visited:
            continue
        visited.add(offset)
        if offset < 4 + count * 8:
            raise ValueError("Cmap subtable points inside records")
        _range(cmap, offset, 2, "cmap subtable")
        fmt = struct.unpack_from(">H", cmap, offset)[0]
        if fmt == 4:
            codes.update(_format4(cmap[offset:], glyph_count))
        elif fmt == 12:
            codes.update(_format12(cmap[offset:], glyph_count))
    codes = {code for code in codes if unicodedata.category(chr(code)) not in ("Cc", "Cs")}
    if not codes:
        raise ValueError("No supported BMP characters")
    return tuple(sorted(codes))


def pack_pixels(pixels: bytes) -> bytes:
    """Nearest 4-bit coverage; first source pixel occupies the high nibble."""
    quantized = pixels.translate(bytes((value + 8) // 17 for value in range(256)))
    if len(quantized) % 2:
        quantized += b"\0"
    return bytes((high << 4) | low for high, low in zip(quantized[::2], quantized[1::2]))


def unpack_pixels(packed: bytes, count: int) -> bytes:
    if count < 0 or len(packed) != (count + 1) // 2:
        raise ValueError("Packed pixel length mismatch")
    if count % 2 and packed[-1] & 15:
        raise ValueError("Nonzero unused final nibble")
    output = bytearray()
    for value in packed:
        output.extend(((value >> 4) * 17, (value & 15) * 17))
    return bytes(output[:count])


def _vlw_metadata(data: bytes, virtual_size: int) -> tuple[int, int]:
    _range(data, 0, 24, "VLW header")
    count, version, size, reserved, ascent, descent = struct.unpack_from(">6I", data)
    bitmap_start = 24 + count * 28
    if not 0 < count <= 65535 or version != 11 or size != FONT_SIZE or reserved or not 0 < ascent + descent < 64:
        raise ValueError("Invalid VLW header")
    _range(data, 0, bitmap_start, "VLW metrics")
    pixels, previous = 0, -1
    for index in range(count):
        code, height, width, advance, top, left, flags = struct.unpack_from(">7i", data, 24 + index * 28)
        if not previous < code <= 0xFFFF or not 0 < height <= 64 or not 0 < width <= 64:
            raise ValueError("Invalid VLW codepoint/dimensions")
        if not 0 <= advance <= 64 or not -64 <= left <= 64 or not -64 <= top <= 64 or flags not in (0, 1):
            raise ValueError("Invalid VLW glyph metrics")
        previous = code
        pixels += height * width
    if bitmap_start + pixels != virtual_size:
        raise ValueError("VLW virtual pixel size mismatch")
    return count, bitmap_start


def quantize_trim_vlw(raw: bytes) -> bytes:
    """Keep metrics equal to the *quantized* ink, including formerly faint edges.

    A 1..8 alpha edge becomes transparent at four bits. Retaining its old box
    would shift visible centering and dirty bounds, so crop after quantization
    and update baseline-relative bearings before constructing the virtual VLW.
    """
    count, cursor = _vlw_metadata(raw, len(raw))
    table = bytes(((value + 8) // 17) * 17 for value in range(256))
    glyphs = []
    for index in range(count):
        code, height, width, advance, top, left, _ = struct.unpack_from(">7i", raw, 24 + index * 28)
        pixels = raw[cursor:cursor + height * width].translate(table)
        cursor += height * width
        ink = [position for position, alpha in enumerate(pixels) if alpha]
        if ink:
            x0 = min(position % width for position in ink)
            x1 = max(position % width for position in ink) + 1
            y0, y1 = ink[0] // width, ink[-1] // width + 1
            cropped = b"".join(pixels[row * width + x0:row * width + x1] for row in range(y0, y1))
            glyphs.append((code, y1 - y0, x1 - x0, advance, top - y0, left + x0, 0, cropped))
        else:
            glyphs.append((code, 1, 1, advance, 0, 0, 1, b"\0"))
    ascent = max(glyph[4] for glyph in glyphs)
    descent = max(glyph[1] - glyph[4] for glyph in glyphs)
    result = (struct.pack(">6I", count, 11, FONT_SIZE, 0, ascent, descent)
              + b"".join(struct.pack(">7i", *glyph[:7]) for glyph in glyphs)
              + b"".join(glyph[7] for glyph in glyphs))
    _vlw_metadata(result, len(result))
    return result


def pack_vlw(raw: bytes) -> bytes:
    count, bitmap_start = _vlw_metadata(raw, len(raw))
    payload = raw[:bitmap_start] + pack_pixels(raw[bitmap_start:])
    total = HEADER_SIZE + len(payload)
    if total > MAX_ASSET_SIZE:
        raise ValueError("Title font exceeds the reserved asset partition")
    header = MAGIC + struct.pack("<6I", 1, total, len(raw), bitmap_start, count, zlib.crc32(payload))
    return header.ljust(HEADER_SIZE, b"\0") + payload


def inspect_asset(data: bytes) -> dict[str, int]:
    _range(data, 0, HEADER_SIZE, "asset header")
    if data[:8] != MAGIC or any(data[32:64]):
        raise ValueError("Invalid asset magic/reserved bytes")
    version, size, virtual_size, bitmap_start, count, crc = struct.unpack_from("<6I", data, 8)
    if version != 1 or size != len(data) or size > MAX_ASSET_SIZE or virtual_size < bitmap_start:
        raise ValueError("Invalid asset version/size")
    if size != HEADER_SIZE + bitmap_start + (virtual_size - bitmap_start + 1) // 2:
        raise ValueError("Invalid packed asset bounds")
    if zlib.crc32(data[HEADER_SIZE:]) != crc:
        raise ValueError("Title font CRC mismatch")
    actual_count, actual_start = _vlw_metadata(data[HEADER_SIZE:], virtual_size)
    if (count, bitmap_start) != (actual_count, actual_start):
        raise ValueError("Asset and VLW metadata disagree")
    if (virtual_size - bitmap_start) % 2 and data[-1] & 15:
        raise ValueError("Nonzero unused final nibble")
    return {"version": version, "file_size": size, "raw_vlw_size": virtual_size,
            "bitmap_start": bitmap_start, "glyph_count": count, "crc32": crc}


def unpack_vlw(data: bytes) -> bytes:
    metadata = inspect_asset(data)
    start = metadata["bitmap_start"]
    return data[64:64 + start] + unpack_pixels(data[64 + start:], metadata["raw_vlw_size"] - start)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Reproduce and compare without writing")
    args = parser.parse_args()
    # Import lazily: cmap/packing tests remain entirely standard-library-only.
    import build_ui_font as ui
    from PIL import ImageFont, __version__ as pillow_version

    if args.check and not ui.CACHE.is_file():
        raise ValueError("Pinned upstream font cache is required for a read-only check")
    path = ui.font_path()
    codes = supported_bmp_codes(path.read_bytes())
    chars = "".join(map(chr, codes))
    raw = quantize_trim_vlw(ui.vlw(ImageFont.truetype(str(path), FONT_SIZE), chars, FONT_SIZE))
    ui.validate(raw, chars)  # Every declared ink edge still survives quantization.
    asset = pack_vlw(raw)
    metadata = inspect_asset(asset)
    manifest = {**metadata, "format": "T5VLW4", "font_size": FONT_SIZE,
                "font_sha256": ui.FONT_SHA256, "font_url": ui.FONT_URL,
                "sha256": hashlib.sha256(asset).hexdigest(), "pillow_version": pillow_version,
                "coverage": "Unicode cmap format 4/12, nonzero glyph IDs, BMP excluding Cc/Cs",
                "quantization": "nearest alpha/17, trim quantized ink, preserve glyph advances",
                "packed_pixel_bytes": len(asset) - HEADER_SIZE - metadata["bitmap_start"],
                "raw_pixel_bytes": len(raw) - metadata["bitmap_start"]}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    manifest_path = args.output.with_suffix(args.output.suffix + ".json")
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != asset:
            raise ValueError("Title font asset differs from deterministic generation")
        if not manifest_path.is_file() or manifest_path.read_bytes() != manifest_bytes:
            raise ValueError("Title font manifest differs from deterministic generation")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(asset)
        manifest_path.write_bytes(manifest_bytes)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, AssertionError) as error:
        print(f"Title font generation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
