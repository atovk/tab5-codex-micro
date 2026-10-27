#include "title_layout.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

uint32_t next(const std::string& text, std::size_t& position) {
  require(position < text.size(), "UTF-8 decoder exceeded the string");
  const uint8_t lead = uint8_t(text[position++]);
  if (lead < 0x80) return lead;
  unsigned continuation = 0;
  uint32_t value = 0, minimum = 0;
  if (lead >= 0xC2 && lead <= 0xDF) {
    continuation = 1; value = lead & 31; minimum = 0x80;
  } else if (lead >= 0xE0 && lead <= 0xEF) {
    continuation = 2; value = lead & 15; minimum = 0x800;
  } else if (lead >= 0xF0 && lead <= 0xF4) {
    continuation = 3; value = lead & 7; minimum = 0x10000;
  } else throw std::runtime_error("invalid UTF-8 lead byte reached measurement");
  require(position + continuation <= text.size(), "truncated UTF-8 reached measurement");
  for (unsigned i = 0; i < continuation; ++i) {
    const uint8_t byte = uint8_t(text[position++]);
    require((byte & 0xC0) == 0x80, "invalid UTF-8 continuation reached measurement");
    value = (value << 6) | (byte & 63);
  }
  require(value >= minimum && value <= 0x10FFFF
              && !(value >= 0xD800 && value <= 0xDFFF),
          "noncanonical UTF-8 reached measurement");
  return value;
}

bool validUtf8(const std::string& text) {
  try {
    std::size_t position = 0;
    while (position < text.size()) next(text, position);
    return true;
  } catch (const std::runtime_error&) { return false; }
}

struct MeasureState { std::size_t calls = 0; bool fail = false; bool kern = false; };

int32_t measure(const char* value, void* context) {
  auto& state = *static_cast<MeasureState*>(context);
  ++state.calls;
  if (state.fail) return -1;
  std::string text(value);
  while (!text.empty() && text.front() == ' ') text.erase(0, 1);
  while (!text.empty() && text.back() == ' ') text.pop_back();
  int32_t width = 0;
  uint32_t previous = 0;
  std::size_t position = 0;
  while (position < text.size()) {
    const auto code = next(text, position);
    // Broad Chinese/emoji glyphs, proportional Latin and a zero-width mark.
    width += code == 0x301 ? 0 : code >= 0x3000 ? 2 : 1;
    if (state.kern && previous == 'A' && code == 'V') --width;
    previous = code;
  }
  return width;
}

std::vector<std::string> displayLines(const TitleLayout::Layout& layout) {
  std::vector<std::string> result;
  for (std::size_t i = 0; i < layout.count; ++i) {
    std::array<char, TitleLayout::MaxBytes + 1> line{};
    require(TitleLayout::copyLine(layout, i, line.data(), line.size()),
            "valid line could not be copied");
    result.emplace_back(line.data());
  }
  return result;
}

void complete(const TitleLayout::Layout& layout, const std::string& expected,
              int32_t maxWidth, bool allowOverflow = false) {
  require(std::string(layout.text) == expected, "normalized source was lost or changed");
  require(layout.count <= TitleLayout::MaxLines, "line count exceeded fixed capacity");
  require(expected.empty() == (layout.count == 0), "empty source has the wrong line count");
  std::string reconstructed;
  std::size_t end = 0;
  MeasureState state;
  for (std::size_t i = 0; i < layout.count; ++i) {
    const auto& line = layout.lines[i];
    require(line.begin == end && line.length,
            "line ranges are not contiguous nonempty source segments");
    require(std::size_t(line.begin) + line.length <= expected.size(),
            "line range escapes its normalized source");
    const std::string bytes(layout.text + line.begin, line.length);
    require(validUtf8(bytes), "line boundary splits a UTF-8 codepoint");
    require(line.width == measure(bytes.c_str(), &state),
            "stored line width differs from the callback's actual measurement");
    if (!allowOverflow) require(line.width <= maxWidth, "a line exceeds its width budget");
    reconstructed += bytes;
    end += line.length;
  }
  require(reconstructed == expected && end == expected.size(),
          "concatenated raw line ranges dropped or duplicated title bytes");
}

void emptyAndInvalidArguments() {
  TitleLayout::Layout layout;
  MeasureState state;
  require(TitleLayout::wrap(layout, "", 8, measure, &state), "empty title was rejected");
  complete(layout, "", 8);
  require(!layout.repaired && !layout.inputTruncated && !layout.overflow,
          "empty title acquired error flags");
  require(!TitleLayout::wrap(layout, nullptr, 8, measure, &state), "null source was accepted");
  require(!TitleLayout::wrap(layout, "title", 0, measure, &state), "zero width was accepted");
  require(!TitleLayout::wrap(layout, "title", -1, measure, &state), "negative width was accepted");
  require(!TitleLayout::wrap(layout, "title", 8, nullptr, &state), "null callback was accepted");
  require(layout.count == 0 && !*layout.text, "invalid request retained an old layout");
}

void exactBoundaryAndLongTokens() {
  MeasureState state;
  TitleLayout::Layout layout;
  require(TitleLayout::wrap(layout, "ABCD", 4, measure, &state), "exact boundary rejected");
  complete(layout, "ABCD", 4);
  require(displayLines(layout) == std::vector<std::string>{"ABCD"},
          "an exact-width title wrapped unnecessarily");
  require(TitleLayout::wrap(layout, "ABCDEFGHIJ", 4, measure, &state), "long token rejected");
  complete(layout, "ABCDEFGHIJ", 4);
  require(displayLines(layout) == std::vector<std::string>({"ABCD", "EFGH", "IJ"}),
          "long unspaced token did not wrap at codepoint boundaries");
}

void englishWordsAndBoundarySpaces() {
  MeasureState state;
  TitleLayout::Layout layout;
  const std::string source = "hello world again";
  require(TitleLayout::wrap(layout, source.c_str(), 7, measure, &state), "English title rejected");
  complete(layout, source, 7);
  require(displayLines(layout) == std::vector<std::string>({"hello", "world", "again"}),
          "English words or display boundary-space trimming were not preserved");
  require(TitleLayout::wrap(layout, "  one  two  ", 5, measure, &state), "spaced title rejected");
  complete(layout, "  one  two  ", 5);
  for (const auto& line : displayLines(layout))
    require(line.empty() || (line.front() != ' ' && line.back() != ' '),
            "copyLine exposed a display boundary space");
  require(TitleLayout::wrap(layout, u8"　one　", 8, measure, &state),
          "ideographic-space title rejected");
  complete(layout, u8"　one　", 8);
  require(displayLines(layout) == std::vector<std::string>{"one"},
          "copyLine did not trim breakable ideographic boundary spaces");
}

void chineseAndMixedUtf8() {
  MeasureState state;
  TitleLayout::Layout layout;
  const std::string chinese = u8"中文标题换行";
  require(TitleLayout::wrap(layout, chinese.c_str(), 4, measure, &state), "CJK title rejected");
  complete(layout, chinese, 4);
  require(displayLines(layout) == std::vector<std::string>({u8"中文", u8"标题", u8"换行"}),
          "Chinese glyph boundaries wrapped incorrectly");
  const std::string mixed = u8"部署 Codex 任务 😀 测试";
  require(TitleLayout::wrap(layout, mixed.c_str(), 8, measure, &state), "mixed title rejected");
  complete(layout, mixed, 8);
  require(!layout.repaired && !layout.inputTruncated, "valid emoji/UTF-8 was normalized unexpectedly");
  require(layout.count > 2, "long mixed text did not expose lines beyond the viewport");
  const std::string combining = u8"Café";
  require(TitleLayout::wrap(layout, combining.c_str(), 4, measure, &state),
          "valid combining sequence rejected");
  complete(layout, combining, 4);
}

void closingPunctuationStaysWithItsPredecessor() {
  MeasureState state;
  TitleLayout::Layout layout;
  const std::string source = u8"你好，世界。再见！";
  require(TitleLayout::wrap(layout, source.c_str(), 4, measure, &state), "punctuated title rejected");
  complete(layout, source, 4);
  for (const auto& line : displayLines(layout)) {
    require(line.rfind(u8"，", 0) != 0 && line.rfind(u8"。", 0) != 0
                && line.rfind(u8"！", 0) != 0,
            "avoidable closing punctuation starts a line");
  }
  // Opening-punctuation protection applies only to a real wrap boundary.
  for (const char* sourceAtEnd : {"AB(", u8"中（"}) {
    require(TitleLayout::wrap(layout, sourceAtEnd, 4, measure, &state),
            "terminal opening-punctuation title rejected");
    complete(layout, sourceAtEnd, 4);
    require(layout.count == 1,
            "an opening punctuation at end created an unnecessary second row");
  }
}

void fullStringInkMeasurementIsUsed() {
  MeasureState state;
  state.kern = true;
  TitleLayout::Layout layout;
  require(TitleLayout::wrap(layout, "AV", 1, measure, &state), "kerning fixture rejected");
  require(layout.count == 1 && layout.lines[0].width == 1,
          "layout summed isolated glyph widths instead of measuring the full string");
  require(std::string(layout.text) == "AV", "kerning fixture dropped text");
}

void malformedUtf8AndControlsAreSafe() {
  MeasureState state;
  TitleLayout::Layout layout;
  for (const std::string& source : {std::string("\xE4\xB8", 2), std::string("\xC0\xAF", 2),
                                 std::string("\xED\xA0\x80", 3), std::string("\xF4\x90\x80\x80", 4),
                                 std::string("\x80\xBF", 2)}) {
    require(TitleLayout::wrap(layout, source.c_str(), 8, measure, &state),
            "malformed source was not safely repaired");
    complete(layout, std::string(source.size(), '?'), 8);
    require(layout.repaired && !layout.inputTruncated, "UTF-8 repair flags are wrong");
  }
  const std::string controls("A\nB\x7F", 4);
  require(TitleLayout::wrap(layout, controls.c_str(), 8, measure, &state), "control repair rejected");
  complete(layout, "A B ", 8);
  require(layout.repaired, "control normalization was not reported");
}

void maximumBytesAndSafeInputTruncation() {
  MeasureState state;
  TitleLayout::Layout layout;
  const std::string maximum(TitleLayout::MaxBytes, 'A');
  require(TitleLayout::wrap(layout, maximum.c_str(), 10, measure, &state), "maximal title rejected");
  complete(layout, maximum, 10);
  require(!layout.inputTruncated && !layout.overflow, "maximal valid title overflowed unexpectedly");
  const std::string exact = std::string(188, 'A') + u8"中";
  require(exact.size() == TitleLayout::MaxBytes, "test fixture byte count changed");
  require(TitleLayout::wrap(layout, exact.c_str(), 10, measure, &state), "maximal UTF-8 title rejected");
  complete(layout, exact, 10);
  require(!layout.inputTruncated, "whole terminal codepoint was truncated");
  const std::string oversized = std::string(190, 'A') + u8"中文";
  require(TitleLayout::wrap(layout, oversized.c_str(), 10, measure, &state), "oversized title rejected");
  complete(layout, std::string(190, 'A'), 10);
  require(layout.inputTruncated && !layout.repaired, "oversized UTF-8 clipping flags are wrong");
}

void boundedLinesNeverDropRemainingContent() {
  MeasureState state;
  TitleLayout::Layout layout;
  const std::string source(TitleLayout::MaxBytes, 'A');
  require(TitleLayout::wrap(layout, source.c_str(), 1, measure, &state), "narrow layout rejected");
  complete(layout, source, 1, true);
  require(layout.count == TitleLayout::MaxLines && layout.overflow
              && layout.lines[layout.count - 1].width > 1,
          "capacity limit did not preserve the remainder in a marked overflow row");
  require(state.calls <= (TitleLayout::MaxBytes + 1) * (TitleLayout::MaxLines + 2),
          "measurement callback count is unbounded for a short title");
  require(TitleLayout::wrap(layout, u8"中文", 1, measure, &state), "overwide glyph rejected");
  complete(layout, u8"中文", 1, true);
  require(layout.overflow, "an overwide glyph was not reported");
}

void copyBoundsAndCallbackFailure() {
  MeasureState state;
  TitleLayout::Layout layout;
  require(TitleLayout::wrap(layout, " title ", 10, measure, &state), "copy fixture rejected");
  std::array<char, 6> exact{};
  require(TitleLayout::copyLine(layout, 0, exact.data(), exact.size())
              && std::string(exact.data()) == "title",
          "copyLine did not trim and NUL-terminate into an exact-capacity buffer");
  std::array<char, 5> small = {{'x', 'x', 'x', 'x', 'x'}};
  require(!TitleLayout::copyLine(layout, 0, small.data(), small.size()),
          "copyLine accepted an undersized buffer");
  require(!TitleLayout::copyLine(layout, 1, exact.data(), exact.size()),
          "copyLine accepted an out-of-range line index");
  require(!TitleLayout::copyLine(layout, std::numeric_limits<std::size_t>::max(), exact.data(), exact.size()),
          "copyLine accepted an overflowing line index");
  require(!TitleLayout::copyLine(layout, 0, nullptr, 99)
              && !TitleLayout::copyLine(layout, 0, exact.data(), 0),
          "copyLine accepted a missing output buffer");
  state.fail = true;
  require(!TitleLayout::wrap(layout, "title", 10, measure, &state),
          "negative callback width was accepted");
  require(layout.count == 0 && !*layout.text, "callback failure retained a partial layout");
}
} // namespace

int main() {
  try {
    emptyAndInvalidArguments();
    exactBoundaryAndLongTokens();
    englishWordsAndBoundarySpaces();
    chineseAndMixedUtf8();
    closingPunctuationStaysWithItsPredecessor();
    fullStringInkMeasurementIsUsed();
    malformedUtf8AndControlsAreSafe();
    maximumBytesAndSafeInputTruncation();
    boundedLinesNeverDropRemainingContent();
    copyBoundsAndCallbackFailure();
    std::cout << "10/10 title layout tests passed\n";
    return 0;
  } catch (const std::exception& failure) {
    std::cerr << failure.what() << '\n';
    return 1;
  }
}
