#!/usr/bin/env python3
"""Generate the small, antialiased Tab5 UI Sans VLW font subset.

Usage: .venv/bin/python tools/build_ui_font.py [--check] [--preview PATH]
Requires Pillow (the checked-in header does not require Python at build time).
The pinned upstream font stays in artifacts/font-cache, outside the firmware.
Only static UI strings are included; build_title_font.py supplies chat titles.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import struct
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, __version__ as PILLOW_VERSION

ROOT = Path(__file__).resolve().parents[1]
FONT_URL = ("https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/"
            "Sans/SubsetOTF/SC/NotoSansSC-Medium.otf")
FONT_SHA256 = "7633f5a016d4dd95e685a69633d818aabc4644c4b08e26bd35b1b30c45ed5dda"
LICENSE_URL = "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/LICENSE"
CACHE = ROOT / "artifacts/font-cache/NotoSansSC-Medium.otf"
HEADER = ROOT / "include/ui_font.h"
LICENSE = ROOT / "licenses/Tab5-UI-Sans-OFL.txt"
SOURCES = (ROOT / "src/micro_ui.cpp", ROOT / "src/micro_games.cpp", ROOT / "include/agent_status.h",
           ROOT / "include/host_feedback.h")
SIZES = (16, 20, 24, 26, 28, 32, 40)
# Retain the complete fixed label vocabulary even when a redesign temporarily
# removes a string. This also prevents a generated header changing each edit.
EXTRA = """
白天 黑夜 昼夜 日间 夜间 浅色 深色 自动 主题 外观 设置 游戏 返回 控制台
主机已连接 等待主机 旋钮 摇杆 当前 聊天 已过期 快速 批准 拒绝 分支 发送
步进 在线 离线 说话中 独立映射
语音 辅助 按住说话 需开启独立键 已按下 正在说话 辅助已按下 松开结束 按下
按住 长按 选择 归中 已归中 向上 向下 向左 向右 左上 左下 右上 右下 行程
计划 模式 侧栏 前进 后退 默认 随桌面映射 电脑麦克风 独立 麦克风 听写
转动 推动 随手掌控 休息一下 玩点有趣的 返回聊天 游戏时光 操作面板
长按跟随 桌面设置 转动切换 按下选择 计划模式 松手归中 方向动作随桌面映射
当前请求 当前聊天 当前消息 切换模式 辅助键 已启用 未启用 右侧辅助键随桌面独立映射
星际守卫 俄罗斯方块 小恐龙跑酷 游戏库 已结束 继续 暂停 重开 游戏结束 已暂停
拖动飞船 躲避敌机 自动射击 消除整行 留出空间 已消除 等级 轮廓是落点预览
长按方向键可连续移动 下一个 轻点旋转 速降立即落到底部 左移 旋转 右移 下落 速降
轻点场地或右侧按钮跳跃 越过仙人掌 跑得越远分数越高 跳跃 选择游戏 随时返回聊天
本次最高 随时暂停 返回聊天也会保留本局进度 得分 生命 最高 点右上角 再来一局
回到游戏 也可以随时返回游戏库或聊天 按住说话 松开结束 语音 辅助 行程
，。、「」：；！？· / % + − — … °
"""


def download(url: str, destination: Path) -> bytes:
    data = urllib.request.urlopen(url, timeout=60).read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return data


def font_path() -> Path:
    data = CACHE.read_bytes() if CACHE.exists() else download(FONT_URL, CACHE)
    digest = hashlib.sha256(data).hexdigest()
    if digest != FONT_SHA256:
        raise ValueError(f"Pinned upstream font SHA mismatch: {digest}")
    return CACHE


def characters(extra: str) -> str:
    # UTF-8 C++ string literals only; avoid Chinese comments increasing the set.
    texts = [EXTRA, extra]
    for source in SOURCES:
        text = source.read_text(encoding="utf-8")
        texts.extend(re.findall(r'"(?:[^"\\]|\\.)*"', text))
    chars = set(chr(code) for code in range(32, 127))
    # Western European titles, including French accents, stay antialiased too.
    chars.update(chr(code) for code in range(0xA0, 0x100))
    chars.update("Œœ‘’“”")
    chars.update(c for text in texts for c in text if 127 <= ord(c) <= 0xFFFF)
    return "".join(sorted(chars))


def vlw(font: ImageFont.FreeTypeFont, chars: str, size: int) -> bytes:
    glyphs = []
    missing_bitmap = bytes(font.getmask("\uffff", mode="L"))
    for char in chars:
        if char != " " and bytes(font.getmask(char, mode="L")) == missing_bitmap:
            raise ValueError(f"Upstream font is missing static UI glyph {char!r}")
        # Baseline-relative metrics match the Processing VLW format read by
        # M5GFX VLWfont: unicode,height,width,advance,top,left,reserved.
        left, top, right, bottom = font.getbbox(char, anchor="ls")
        width, height = right - left, bottom - top
        advance = int(font.getlength(char) + 0.5)
        bitmap = Image.new("L", (max(1, width), max(1, height)), 0)
        if width and height:
            ImageDraw.Draw(bitmap).text((-left, -top), char, font=font,
                                        fill=255, anchor="ls", stroke_width=0)
        ink = bitmap.getbbox()
        if ink:
            # Pillow layout boxes include bearings and blank edge columns.
            # Trim to nonzero coverage so VLW metrics describe actual ink.
            left += ink[0]
            top += ink[1]
            bitmap = bitmap.crop(ink)
            width, height = bitmap.size
        else:
            # Nonempty transparent data is safe with stock M5GFX's do-loop.
            width = height = 1
            left = top = 0
            bitmap = Image.new("L", (1, 1), 0)
        pixels = bitmap.tobytes()
        if not (0 <= width <= 255 and 0 <= advance <= 255 and -128 <= left <= 127):
            raise ValueError(f"VLW metric out of range: {char!r}")
        # VLW's unused final metric stores our transparent-glyph flag. M5GFX
        # ignores it; UiFont::glyphMetrics uses it to exclude blank spacing.
        glyphs.append((ord(char), height, width, advance, -top, left, int(not ink), pixels))
    ascent = max(g[4] for g in glyphs)
    descent = max(g[1] - g[4] for g in glyphs)
    header = struct.pack(">6I", len(glyphs), 11, size, 0, ascent, descent)
    metrics = b"".join(struct.pack(">7i", *g[:7]) for g in glyphs)
    pixels = b"".join(g[7] for g in glyphs)
    result = header + metrics + pixels
    validate(result, chars)
    return result


def validate(data: bytes, chars: str) -> None:
    count, version, size, _, ascent, descent = struct.unpack_from(">6I", data)
    assert count == len(chars) and version == 11 and 0 < ascent + descent < 64
    cursor = 24 + count * 28
    has_antialias = False
    for i, char in enumerate(chars):
        code, height, width, advance, top, left, flags = struct.unpack_from(">7i", data, 24 + i * 28)
        assert code == ord(char) and height > 0 and width > 0 and advance >= 0
        bitmap = data[cursor:cursor + width * height]
        assert len(bitmap) == width * height
        if flags & 1:
            assert not any(bitmap)
        else:
            # Every declared ink edge must contain actual coverage. This is
            # the guarantee used by UiText's visual centering and damage rects.
            assert any(bitmap[:width]) and any(bitmap[-width:])
            assert any(bitmap[row * width] for row in range(height))
            assert any(bitmap[(row + 1) * width - 1] for row in range(height))
        has_antialias |= any(0 < value < 255 for value in bitmap)
        cursor += width * height
    assert cursor == len(data) and has_antialias


def verify_ink_layout(data: bytes) -> None:
    """Compare metric unions to rasterized nonzero pixels, as LGFX lays out TL."""
    count, _, _, _, ascent, _ = struct.unpack_from(">6I", data)
    cursor = 24 + count * 28
    glyphs = {}
    for i in range(count):
        code, height, width, advance, top, left, flags = struct.unpack_from(">7i", data, 24 + i * 28)
        bitmap = Image.frombytes("L", (width, height), data[cursor:cursor + width * height])
        glyphs[chr(code)] = (left, ascent - top, width, height, advance, flags, bitmap)
        cursor += width * height
    strings = ("CODEX", "micro", "归中", "−24", "0%", "Équipe française", "jazz",
               "  Àœ中 j", "旋钮摇杆", "AG00 · ACT12", "白天 / 黑夜", "  ", "\u00a0")
    for text in strings:
        first = glyphs[text[0]]
        pen = max(0, -first[0])  # LGFX draw_string first negative bearing.
        image = Image.new("L", (2000, 100), 0)
        expected = None
        for char in text:
            left, top, width, height, advance, flags, bitmap = glyphs[char]
            if not flags & 1:
                rect = (pen + left, top, pen + left + width, top + height)
                expected = rect if expected is None else (
                    min(expected[0], rect[0]), min(expected[1], rect[1]),
                    max(expected[2], rect[2]), max(expected[3], rect[3]))
                image.paste(bitmap, (pen + left, top))
            pen += advance
        assert image.getbbox() == expected, (text, image.getbbox(), expected)
        if expected:
            x0, y0, x1, y1 = expected
            cx, cy = 640, 360
            origin_x, origin_y = cx - x0 - (x1 - x0) // 2, cy - y0 - (y1 - y0) // 2
            assert abs(origin_x + (x0 + x1 - 1) / 2 - cx) <= .5
            assert abs(origin_y + (y0 + y1 - 1) / 2 - cy) <= .5


def array(name: str, data: bytes) -> str:
    lines = [f"inline const uint8_t {name}[] PROGMEM = {{"]
    lines.extend("  " + ",".join(f"0x{b:02x}" for b in data[i:i + 20]) + ","
                 for i in range(0, len(data), 20))
    lines.append("};")
    return "\n".join(lines)


def header(chars: str, faces: dict[int, bytes]) -> str:
    arrays = "\n\n".join(array(f"sans{size}Data", data) for size, data in faces.items())
    accessors = "\n".join(
        f"inline Face& face{size}() {{\n"
        f"  static Face face(sans{size}Data, sizeof(sans{size}Data));\n"
        f"  return face;\n}}" for size in faces)
    initializers = "\n".join(f"  ok = detail::face{size}().begin() && ok;" for size in faces)
    selectors = "\n".join(
        f"inline const lgfx::IFont* sans{size}() {{\n"
        f"  auto& face = detail::face{size}();\n"
        f"  return face.loaded ? static_cast<const lgfx::IFont*>(&face.font) : &fonts::efontCN_24;\n"
        f"}}" for size in faces)
    finders = "\n".join(
        f"  if (font == &face{size}().font && face{size}().loaded) return &face{size}();"
        for size in faces)
    sizes = "/".join(str(size) for size in faces)
    return f"""// Generated by tools/build_ui_font.py; do not hand-edit glyph data.
// Tab5 UI Sans: a raster subset derived from Noto Sans SC Medium, Sans2.004.
// Copyright 2014-2021 Adobe (http://www.adobe.com/). SIL OFL 1.1.
// License and source attribution: licenses/Tab5-UI-Sans-OFL.txt
// Upstream SHA256: {FONT_SHA256}
// {len(chars)} glyphs; native {sizes}px; 8-bit antialiasing. Pillow {PILLOW_VERSION}.
// Glyph bitmaps are trimmed to nonzero coverage for visible-ink alignment.
// Static labels only: arbitrary chat titles must retain efontCN fallback.
#pragma once
#include <M5GFX.h>

namespace UiFont {{
struct GlyphMetrics {{
  int16_t x = 0, y = 0, w = 0, h = 0, advance = 0;
  bool ink = false;
}};
namespace detail {{
{arrays}

// The wrapper outlives the font. Neither object is owned by a canvas, so
// canvas.setFont(otherFont) cannot destroy this persistent parsed VLW face.
struct Face {{
  const uint8_t* bytes;
  lgfx::PointerWrapper data;
  lgfx::VLWfont font;
  bool loaded = false;
  bool attempted = false;
  Face(const uint8_t* source, uint32_t length) : bytes(source), data(source, length) {{}}
  bool begin() {{
    if (!attempted) {{
      attempted = true;
      loaded = font.loadFont(&data);
    }}
    return loaded;
  }}
}};
{accessors}
inline const Face* find(const lgfx::IFont* font) {{
{finders}
  return nullptr;
}}
inline int32_t signedWord(const uint8_t* bytes) {{
  const uint32_t value = (uint32_t(bytes[0]) << 24) | (uint32_t(bytes[1]) << 16)
                     | (uint32_t(bytes[2]) << 8) | uint32_t(bytes[3]);
  return static_cast<int32_t>(value);
}}
}} // namespace detail

// Call once from UI initialization, outside inputMux. Rendering and font
// selection then allocate nothing. Use these faces only on the render task.
inline bool begin() {{
  bool ok = true;
{initializers}
  return ok;
}}
{selectors}

// Native-size, TL_DATUM offsets; advance includes spacing, bounds exclude it.
// Read only fixed metadata from mapped flash; no glyph bitmap decode or malloc.
inline bool glyphMetrics(const lgfx::IFont* font, uint16_t codepoint, GlyphMetrics& output) {{
  const auto* face = detail::find(font);
  if (!face) return false;
  uint16_t index = 0;
  if (!face->font.getUnicodeIndex(codepoint, &index)) return false;
  const uint8_t* glyph = face->bytes + 24 + uint32_t(index) * 28;
  output.x = detail::signedWord(glyph + 20);
  output.y = int16_t(face->font.maxAscent - detail::signedWord(glyph + 16));
  output.w = detail::signedWord(glyph + 8);
  output.h = detail::signedWord(glyph + 4);
  output.advance = detail::signedWord(glyph + 12);
  output.ink = !(detail::signedWord(glyph + 24) & 1);
  return true;
}}
inline bool contains(const lgfx::IFont* font, uint16_t codepoint) {{
  GlyphMetrics glyph;
  return glyphMetrics(font, codepoint, glyph);
}}
inline bool contains(uint16_t codepoint) {{
  return contains(sans24(), codepoint);
}}
}} // namespace UiFont
"""


def preview(path: Path, font: Path) -> None:
    half_height = 80 + len(SIZES) * 62
    image = Image.new("RGB", (1400, half_height * 2), "#e9edf2")
    draw = ImageDraw.Draw(image)
    for half in range(2):
        top = half * half_height
        if half:
            draw.rectangle((0, top, 1400, top + half_height), fill="#111722")
        foreground = "#263345" if not half else "#edf3fc"
        draw.text((28, top + 15), "Tab5 UI Sans — native-size antialiasing",
                  font=ImageFont.truetype(str(font), 24), fill=foreground)
        for row, size in enumerate(SIZES):
            y = top + 70 + row * 62
            draw.text((28, y), f"{size}px", font=ImageFont.truetype(str(font), 20), fill=foreground)
            draw.text((115, y), "CODEX  micro  控制台  旋钮  归中  −24  0%  Équipe française",
                      font=ImageFont.truetype(str(font), size), fill=foreground)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the checked-in header is current")
    parser.add_argument("--extra-text", default="", help="additional fixed interface vocabulary")
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    font = font_path()
    chars = characters(args.extra_text)
    faces = {size: vlw(ImageFont.truetype(str(font), size), chars, size) for size in SIZES}
    for face in faces.values():
        verify_ink_layout(face)
    generated = header(chars, faces)
    if args.check:
        if not HEADER.exists() or HEADER.read_text(encoding="utf-8") != generated:
            print("UI font header is stale; run tools/build_ui_font.py", file=sys.stderr)
            return 1
        if not LICENSE.exists() or "SIL OPEN FONT LICENSE Version 1.1" not in LICENSE.read_text(encoding="utf-8"):
            print("UI font OFL attribution is missing", file=sys.stderr)
            return 1
    else:
        HEADER.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEADER.with_suffix(".h.tmp")
        temporary.write_text(generated, encoding="utf-8")
        temporary.replace(HEADER)
        if not LICENSE.exists():
            text = urllib.request.urlopen(LICENSE_URL, timeout=30).read().decode("utf-8")
            LICENSE.parent.mkdir(parents=True, exist_ok=True)
            LICENSE.write_text(
                "Tab5 UI Sans — derived grayscale bitmap subset\n"
                "Source: Noto Sans SC Medium, Sans2.004\n"
                "Copyright 2014-2021 Adobe (http://www.adobe.com/).\n"
                f"Upstream font: {FONT_URL}\nSHA256: {FONT_SHA256}\n"
                "The original font is not bundled; only the generated UI glyph subset is embedded.\n\n"
                + text, encoding="utf-8")
    if args.preview:
        preview(args.preview, font)
    sizes = ", ".join(f"{size}px={len(data)}B" for size, data in faces.items())
    print(f"UI Sans ready: {len(chars)} glyphs; {sizes}; total={sum(map(len, faces.values()))}B; "
          f"{HEADER.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
