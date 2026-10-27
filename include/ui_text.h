#pragma once

#include "ui_font.h"
#include "title_font.h"
#include <algorithm>
#include <cstdint>
#include <limits>

// Single-line, native-size text placement. For UiFont faces, bounds describe
// actual nonzero-alpha ink, including mixed Latin/Chinese and leading spaces.
// Other fonts or missing glyphs use conservative font metrics instead.
namespace UiText {
struct Rect { int32_t x = 0, y = 0, w = 0, h = 0; };

namespace detail {
inline int32_t bounded(int64_t value) {
  return int32_t(std::max<int64_t>(std::numeric_limits<int32_t>::min(),
      std::min<int64_t>(std::numeric_limits<int32_t>::max(), value)));
}
inline int32_t add(int32_t a, int32_t b) { return bounded(int64_t(a) + b); }

// Consume exactly one codepoint, or one bad byte as a replacement character.
// Continuation reads stop at NUL; truncated, overlong and surrogate encodings
// cannot escape the string. No temporary strings or heap allocations.
inline bool next(const char*& cursor, uint32_t& codepoint, bool& valid) {
  const auto* bytes = reinterpret_cast<const uint8_t*>(cursor);
  const uint8_t lead = bytes[0];
  if (!lead) return false;
  if (lead < 0x80) { codepoint = lead; ++cursor; return true; }
  unsigned count = 0;
  uint32_t minimum = 0;
  if (lead >= 0xC2 && lead <= 0xDF) {
    count = 2; codepoint = lead & 0x1F; minimum = 0x80;
  } else if (lead >= 0xE0 && lead <= 0xEF) {
    count = 3; codepoint = lead & 0x0F; minimum = 0x800;
  } else if (lead >= 0xF0 && lead <= 0xF4) {
    count = 4; codepoint = lead & 7; minimum = 0x10000;
  }
  bool okay = count != 0;
  for (unsigned i = 1; okay && i < count; ++i) {
    const uint8_t byte = bytes[i];
    if (!byte || (byte & 0xC0) != 0x80) { okay = false; break; }
    codepoint = (codepoint << 6) | (byte & 0x3F);
  }
  okay = okay && codepoint >= minimum && codepoint <= 0x10FFFF
      && !(codepoint >= 0xD800 && codepoint <= 0xDFFF);
  if (!okay) {
    valid = false;
    codepoint = 0xFFFD;
    ++cursor;
  } else {
    cursor += count;
  }
  return true;
}
inline bool ignored(uint32_t codepoint) {
  // LGFXBase::draw_string ignores controls and BMP variation selectors.
  return codepoint < 0x20 || (codepoint >= 0xFE00 && codepoint < 0xFE10);
}
inline const lgfx::IFont* face(const lgfx::IFont* font) {
  return font ? font : &fonts::Font0;
}

struct Layout { Rect ink; bool validUtf8 = true; };
inline Layout measure(const lgfx::IFont* font, const char* text) {
  Layout result;
  if (!text || !*text) return result;
  font = face(font);
  lgfx::FontMetrics metrics {};
  font->getDefaultMetric(&metrics);
  int32_t pen = 0, left = 0, top = 0, right = 0, bottom = 0;
  bool first = true, haveInk = false;
  uint32_t codepoint = 0;
  const char* cursor = text;
  while (next(cursor, codepoint, result.validUtf8)) {
    if (ignored(codepoint)) continue;
    const bool supported = codepoint <= 0xFFFF
        && font->updateFontMetric(&metrics, uint16_t(codepoint));
    if (!supported) font->updateFontMetric(&metrics, 0);
    // draw_string offsets the whole line only for its first rendered glyph's
    // negative bearing. A leading blank still counts as that first glyph.
    if (first) { pen = std::max<int32_t>(0, -int32_t(metrics.x_offset)); first = false; }

    UiFont::GlyphMetrics glyph {};
    const bool exact = supported
        && (UiFont::glyphMetrics(font, uint16_t(codepoint), glyph)
            || UiTitleFont::glyphMetrics(font, uint16_t(codepoint), glyph));
    if (!exact) {
      glyph.x = metrics.x_offset;
      glyph.y = -std::max<int16_t>(0, metrics.y_offset);
      glyph.w = std::max<int16_t>(metrics.width, metrics.x_advance);
      glyph.h = metrics.height + std::max<int16_t>(0, metrics.y_offset);
      glyph.advance = metrics.x_advance;
      glyph.ink = codepoint != 0x20 && glyph.w > 0 && glyph.h > 0;
    }
    if (glyph.ink && glyph.w > 0 && glyph.h > 0) {
      const int32_t x = add(pen, glyph.x);
      const int32_t r = add(x, glyph.w), b = add(glyph.y, glyph.h);
      if (!haveInk) { left = x; top = glyph.y; right = r; bottom = b; haveInk = true; }
      else {
        left = std::min(left, x); top = std::min<int32_t>(top, glyph.y);
        right = std::max(right, r); bottom = std::max(bottom, b);
      }
    }
    pen = add(pen, std::max<int32_t>(0, glyph.advance));
  }
  if (haveInk) result.ink = {left, top, bounded(int64_t(right) - left),
                            bounded(int64_t(bottom) - top)};
  return result;
}

struct StyleGuard {
  M5Canvas& canvas;
  lgfx::TextStyle original;
  StyleGuard(M5Canvas& target, uint32_t rgb888)
      : canvas(target), original(target.getTextStyle()) {
    auto style = original;
    style.size_x = style.size_y = 1;
    style.datum = lgfx::textdatum_t::top_left;
    style.padding_x = 0;
    style.utf8 = true;
    style.cp437 = false;
    style.fore_rgb888 = style.back_rgb888 = rgb888 & 0xFFFFFF;
    canvas.setTextStyle(style);
  }
  ~StyleGuard() { canvas.setTextStyle(original); }
};

// Only malformed UTF-8 takes this slower path. Draw normalized codepoints
// directly so a truncated sequence cannot leave M5GFX's UTF-8 decoder pending.
inline void drawSanitized(M5Canvas& canvas, const char* text, int32_t x, int32_t y,
                          const lgfx::IFont* font) {
  lgfx::FontMetrics metrics {};
  font->getDefaultMetric(&metrics);
  const char* cursor = text;
  uint32_t codepoint = 0;
  bool valid = true, first = true;
  int32_t pen = 0, filled = 0;
  canvas.startWrite();
  while (next(cursor, codepoint, valid)) {
    if (ignored(codepoint)) continue;
    if (codepoint > 0xFFFF || !font->updateFontMetric(&metrics, uint16_t(codepoint))) {
      codepoint = 0;
      font->updateFontMetric(&metrics, 0);
    }
    if (first) { pen = std::max<int32_t>(0, -int32_t(metrics.x_offset)); first = false; }
    const size_t advance = font->drawChar(&canvas, add(x, pen),
        add(y, -int32_t(metrics.y_offset)), uint16_t(codepoint),
        &canvas.getTextStyle(), &metrics, filled);
    pen = add(pen, bounded(advance));
  }
  canvas.endWrite();
}
inline Rect absolute(Rect rect, int32_t x, int32_t y) {
  rect.x = add(rect.x, x); rect.y = add(rect.y, y);
  return rect;
}
} // namespace detail

// Coordinates are relative to the TL_DATUM line origin used by drawString.
// Empty/all-blank strings return {0,0,0,0}. Exact for the generated UiFont
// faces; other fonts and missing glyphs return a conservative metric rectangle.
inline Rect bounds(const lgfx::IFont* font, const char* text) {
  return detail::measure(font, text).ink;
}

inline Rect drawAt(M5Canvas& canvas, const char* text, int32_t x, int32_t y,
                   const lgfx::IFont* font, uint32_t rgb888) {
  font = detail::face(font);
  const auto layout = detail::measure(font, text);
  if (text && *text) {
    detail::StyleGuard style(canvas, rgb888);
    // The explicit-font overload preserves the canvas's selected/runtime font.
    if (layout.validUtf8) canvas.drawString(text, x, y, font);
    else detail::drawSanitized(canvas, text, x, y, font);
  }
  return detail::absolute(layout.ink, x, y);
}

inline Rect drawLeft(M5Canvas& canvas, const char* text, int32_t x, int32_t y,
                     const lgfx::IFont* font, uint32_t rgb888) {
  const Rect ink = bounds(font, text);
  return drawAt(canvas, text, detail::add(x, -ink.x), detail::add(y, -ink.y), font, rgb888);
}

inline Rect drawCentered(M5Canvas& canvas, const char* text, int32_t cx, int32_t cy,
                         const lgfx::IFont* font, uint32_t rgb888) {
  const Rect ink = bounds(font, text);
  return drawAt(canvas, text, detail::bounded(int64_t(cx) - ink.x - ink.w / 2),
      detail::bounded(int64_t(cy) - ink.y - ink.h / 2), font, rgb888);
}

// x is the exclusive right ink edge; y is the visible top edge.
inline Rect drawRight(M5Canvas& canvas, const char* text, int32_t x, int32_t y,
                      const lgfx::IFont* font, uint32_t rgb888) {
  const Rect ink = bounds(font, text);
  return drawAt(canvas, text, detail::bounded(int64_t(x) - ink.x - ink.w),
      detail::add(y, -ink.y), font, rgb888);
}
} // namespace UiText
