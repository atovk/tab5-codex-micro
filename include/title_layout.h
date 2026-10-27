#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

// Bounded, allocation-free title wrapping. Animation policy and font/glyph
// normalization belong to the renderer; this module only produces UTF-8 ranges.
namespace TitleLayout {
inline constexpr std::size_t MaxBytes = 191;
inline constexpr std::size_t MaxLines = 64;

struct Line {
  uint16_t begin = 0, length = 0;
  int32_t width = 0;
};
struct Layout {
  char text[MaxBytes + 1] = {};
  Line lines[MaxLines] = {};
  uint8_t count = 0;
  bool repaired = false;
  bool inputTruncated = false;
  bool overflow = false;
};
using Measure = int32_t (*)(const char* utf8, void* context);

namespace detail {
struct Point { uint32_t code; uint16_t begin, end; };

// Consume a valid scalar, or one invalid byte as '?'. Continuation reads stop
// at NUL, so a malformed final sequence never reads beyond its C string.
inline std::size_t decode(const char* input, uint32_t& code, bool& valid) {
  const auto* bytes = reinterpret_cast<const uint8_t*>(input);
  const uint8_t lead = bytes[0];
  valid = true;
  if (lead < 0x80) { code = lead; return lead ? 1 : 0; }
  unsigned count = 0;
  uint32_t minimum = 0;
  if (lead >= 0xC2 && lead <= 0xDF) {
    count = 2; code = lead & 0x1F; minimum = 0x80;
  } else if (lead >= 0xE0 && lead <= 0xEF) {
    count = 3; code = lead & 0x0F; minimum = 0x800;
  } else if (lead >= 0xF0 && lead <= 0xF4) {
    count = 4; code = lead & 7; minimum = 0x10000;
  }
  bool okay = count != 0;
  for (unsigned i = 1; okay && i < count; ++i) {
    const uint8_t byte = bytes[i];
    if (!byte || (byte & 0xC0) != 0x80) { okay = false; break; }
    code = (code << 6) | (byte & 0x3F);
  }
  if (!okay || code < minimum || code > 0x10FFFF
      || (code >= 0xD800 && code <= 0xDFFF)) {
    code = '?'; valid = false; return 1;
  }
  return count;
}

inline bool space(uint32_t code) { return code == ' ' || code == 0x3000; }
inline bool eastAsian(uint32_t code) {
  return (code >= 0x2E80 && code <= 0xA4CF)
      || (code >= 0xAC00 && code <= 0xD7AF)
      || (code >= 0xF900 && code <= 0xFAFF)
      || (code >= 0xFF00 && code <= 0xFFEF)
      || (code >= 0x20000 && code <= 0x323AF);
}
inline bool closing(uint32_t code) {
  switch (code) {
    case ')': case ']': case '}': case ',': case '.': case '!': case '?':
    case ':': case ';': case '%': case 0x2019: case 0x201D: case 0x2026:
    case 0x3001: case 0x3002: case 0x3009: case 0x300B: case 0x300D:
    case 0x300F: case 0x3011: case 0x3015: case 0x3017: case 0x3019:
    case 0x301B: case 0xFF01: case 0xFF05: case 0xFF09: case 0xFF0C:
    case 0xFF0E: case 0xFF1A: case 0xFF1B: case 0xFF1F: case 0xFF3D:
    case 0xFF5D: return true;
    default: return false;
  }
}
inline bool opening(uint32_t code) {
  switch (code) {
    case '(': case '[': case '{': case 0x2018: case 0x201C: case 0x3008:
    case 0x300A: case 0x300C: case 0x300E: case 0x3010: case 0x3014:
    case 0x3016: case 0x3018: case 0x301A: case 0xFF08: case 0xFF3B:
    case 0xFF5B: return true;
    default: return false;
  }
}
inline bool preferred(uint32_t before, uint32_t after) {
  if (closing(after) || opening(before) || space(after)) return false;
  return space(before) || eastAsian(before) || eastAsian(after);
}
inline bool measure(const char* text, uint16_t begin, uint16_t end,
                    Measure callback, void* context, int32_t& width) {
  char fragment[MaxBytes + 1];
  const std::size_t length = end - begin;
  std::memcpy(fragment, text + begin, length);
  fragment[length] = 0;
  width = callback(fragment, context);
  return width >= 0;
}
inline bool failure(Layout& result) { result = Layout{}; return false; }
} // namespace detail

// Preserve all accepted bytes in contiguous line ranges. Spaces at boundaries
// remain in text/ranges and are trimmed only by copyLine for display. Input is
// normally pre-normalized; malformed UTF-8 becomes '?', C0/DEL becomes a space.
// Oversized input is clipped at a complete scalar and flagged. If a pathological
// width needs more than 64 rows, the final row retains the complete remainder
// and overflow is set; no accepted title content is discarded or ellipsized.
inline bool wrap(Layout& result, const char* normalized, int32_t maxWidth,
                 Measure callback, void* context = nullptr) {
  result = Layout{};
  if (!normalized || !callback || maxWidth <= 0) return false;
  detail::Point points[MaxBytes];
  std::size_t count = 0, written = 0;
  const char* cursor = normalized;
  while (*cursor) {
    uint32_t code = 0;
    bool valid = true;
    const std::size_t consumed = detail::decode(cursor, code, valid);
    const bool control = code < 0x20 || code == 0x7F;
    const std::size_t bytes = valid && !control ? consumed : 1;
    if (written + bytes > MaxBytes) { result.inputTruncated = true; break; }
    points[count].begin = uint16_t(written);
    if (!valid || control) {
      code = control ? ' ' : '?';
      result.text[written++] = char(code);
      result.repaired = true;
    } else {
      std::memcpy(result.text + written, cursor, bytes);
      written += bytes;
    }
    points[count].code = code;
    points[count++].end = uint16_t(written);
    cursor += consumed;
  }
  result.text[written] = 0;
  std::size_t start = 0;
  while (start < count) {
    const uint16_t byteBegin = points[start].begin;
    std::size_t end = start, fitted = start, preferred = start;
    int32_t fittedWidth = 0, firstWidth = 0;
    bool haveText = false;
    for (end = start + 1; end <= count; ++end) {
      int32_t width = 0;
      if (!detail::measure(result.text, byteBegin, points[end - 1].end,
                           callback, context, width)) return detail::failure(result);
      if (end == start + 1) firstWidth = width;
      if (width > maxWidth) break;
      fitted = end;
      fittedWidth = width;
      haveText = haveText || !detail::space(points[end - 1].code);
      if (haveText && end < count
          && detail::preferred(points[end - 1].code, points[end].code)) preferred = end;
    }
    end = fitted == count ? fitted : preferred > start ? preferred : fitted;
    if (end == start) end = start + 1;  // One overwide glyph must still progress.

    // A closing-punctuation run stays with its preceding glyph when that
    // cluster fits. If the cluster itself is too wide, breaking it is unavoidable.
    if (end < count && detail::closing(points[end].code)) {
      std::size_t cluster = end - 1, after = end;
      while (cluster > start && detail::closing(points[cluster].code)) --cluster;
      while (after < count && detail::closing(points[after].code)) ++after;
      if (cluster > start) {
        int32_t width = 0;
        if (!detail::measure(result.text, points[cluster].begin,
                             points[after - 1].end, callback, context, width))
          return detail::failure(result);
        if (width <= maxWidth) end = cluster;
      }
    }
    while (end < count && end > start + 1
        && detail::opening(points[end - 1].code)) --end;

    if (result.count == MaxLines - 1 && end < count) {
      end = count;
      result.overflow = true;
    }
    int32_t width = end == fitted ? fittedWidth : end == start + 1 ? firstWidth : 0;
    if (end != fitted && end != start + 1
        && !detail::measure(result.text, byteBegin, points[end - 1].end,
                            callback, context, width)) return detail::failure(result);
    auto& line = result.lines[result.count++];
    line.begin = byteBegin;
    line.length = uint16_t(points[end - 1].end - byteBegin);
    line.width = width;
    if (width > maxWidth) result.overflow = true;
    start = end;
  }
  return true;
}

// Copy a display row, trimming only breakable boundary spaces. Raw ranges stay
// untouched. Insufficient capacity returns false and leaves an empty C string;
// no partial UTF-8 scalar is ever emitted.
inline bool copyLine(const Layout& layout, std::size_t index, char* destination,
                     std::size_t capacity) {
  if (!destination || !capacity) return false;
  destination[0] = 0;
  if (index >= layout.count || index >= MaxLines) return false;
  const auto& line = layout.lines[index];
  const std::size_t end = std::size_t(line.begin) + line.length;
  if (end > MaxBytes) return false;
  std::size_t first = end, last = line.begin, offset = line.begin;
  while (offset < end) {
    uint32_t code = 0;
    bool valid = true;
    const std::size_t bytes = detail::decode(layout.text + offset, code, valid);
    if (!bytes || !valid || bytes > end - offset) return false;
    if (!detail::space(code)) {
      if (first == end) first = offset;
      last = offset + bytes;
    }
    offset += bytes;
  }
  const std::size_t length = first < last ? last - first : 0;
  if (capacity <= length) return false;
  if (length) std::memcpy(destination, layout.text + first, length);
  destination[length] = 0;
  return true;
}
} // namespace TitleLayout
