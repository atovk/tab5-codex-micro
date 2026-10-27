#include "title_font_asset.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using Bytes = std::vector<uint8_t>;
using TitleFontAsset::View;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void little32(Bytes& bytes, size_t offset, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i)
    bytes.at(offset + i) = uint8_t(value >> (i * 8));
}

void big32(Bytes& bytes, size_t offset, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i)
    bytes.at(offset + i) = uint8_t(value >> ((3 - i) * 8));
}

void refreshCrc(Bytes& bytes) {
  little32(bytes, 28, TitleFontAsset::crc32(bytes.data() + 64, bytes.size() - 64));
}

struct Fixture {
  Bytes stored;
  Bytes raw;
  uint32_t bitmapStart = 24 + 3 * 28;
};

Fixture fixture(bool oddPixels = false) {
  Fixture result;
  const std::array<uint8_t, 8> nibbles = {{0, 1, 2, 3, 4, 5, 14, 15}};
  result.raw.resize(result.bitmapStart, 0);
  big32(result.raw, 0, 3);   // Three sorted BMP glyphs.
  big32(result.raw, 4, 11);  // VLW version.
  big32(result.raw, 8, 24);  // Native size.
  big32(result.raw, 16, 24);
  big32(result.raw, 20, 4);
  const int32_t glyphs[3][7] = {
      {0x20, 1, 1, 6, 0, 0, 1},  // Transparent space with actual bitmap storage.
      {0x41, 1, oddPixels ? 2 : 3, 4, 2, -1, 0},
      {0x4E2D, 2, 2, 3, 2, 0, 0},
  };
  for (size_t i = 0; i < 3; ++i)
    for (size_t field = 0; field < 7; ++field)
      big32(result.raw, 24 + i * 28 + field * 4, uint32_t(glyphs[i][field]));
  const size_t count = oddPixels ? 7 : 8;
  for (size_t i = 0; i < count; ++i) result.raw.push_back(uint8_t(nibbles[i] * 17));

  result.stored.resize(64, 0);
  const uint8_t magic[8] = {'T', '5', 'V', 'L', 'W', '4', 0, 0};
  std::copy(std::begin(magic), std::end(magic), result.stored.begin());
  result.stored.insert(result.stored.end(), result.raw.begin(),
                       result.raw.begin() + result.bitmapStart);
  for (size_t i = 0; i < count; i += 2) {
    // The final unused low nibble is canonical zero padding, not another pixel.
    const uint8_t low = i + 1 < count ? nibbles[i + 1] : 0;
    result.stored.push_back(uint8_t((nibbles[i] << 4) | low));
  }
  little32(result.stored, 8, 1);
  little32(result.stored, 12, uint32_t(result.stored.size()));
  little32(result.stored, 16, uint32_t(result.raw.size()));
  little32(result.stored, 20, result.bitmapStart);
  little32(result.stored, 24, 3);
  refreshCrc(result.stored);
  return result;
}

void rejected(const Bytes& bytes, const std::string& reason) {
  View view;
  require(!view.begin(bytes.data(), bytes.size()), "accepted " + reason);
  require(!view.valid(), "failed validation left a valid view: " + reason);
  std::array<uint8_t, 4> output = {{9, 9, 9, 9}};
  require(view.read(0, output.data(), output.size()) == 0,
          "failed validation exposed bytes: " + reason);
  TitleFontAsset::Glyph glyph{};
  require(!view.glyph(0x41, glyph), "failed validation exposed glyph: " + reason);
}

void crcMatchesStandardVector() {
  const uint8_t text[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
  require(TitleFontAsset::crc32(text, sizeof(text)) == 0xCBF43926u,
          "CRC32 must match the standard IEEE check vector");
  require(TitleFontAsset::crc32(nullptr, 0) == 0,
          "empty payload CRC32 must be zero");
}

void validHeaderAndMetrics() {
  const auto value = fixture();
  View view;
  require(view.begin(value.stored.data(), value.stored.size()) && view.valid(),
          "valid fixture was rejected");
  require(view.storedBytes() == value.stored.size()
              && view.rawBytes() == value.raw.size()
              && view.bitmapStart() == value.bitmapStart && view.glyphCount() == 3,
          "validated header dimensions were not retained");
  TitleFontAsset::Glyph glyph{};
  require(view.glyph(0x20, glyph) && glyph.codepoint == 0x20
              && glyph.width == 1 && glyph.height == 1 && glyph.advance == 6
              && !glyph.ink,
          "space's transparent metadata was lost");
  require(view.glyph(0x41, glyph) && glyph.codepoint == 0x41
              && glyph.width == 3 && glyph.height == 1 && glyph.advance == 4
              && glyph.top == 2 && glyph.left == -1 && glyph.ink,
          "signed bearing or glyph dimensions were not decoded");
  require(view.glyph(0x4E2D, glyph) && glyph.width == 2 && glyph.height == 2
              && glyph.advance == 3 && glyph.top == 2 && glyph.left == 0,
          "binary search must find the final BMP glyph");
  for (const uint16_t missing : {uint16_t(0), uint16_t(0x21), uint16_t(0x4E2E),
                                  uint16_t(0xFFFF)})
    require(!view.glyph(missing, glyph), "missing codepoint was invented");
  auto zeroAdvance = value.stored;
  big32(zeroAdvance, 64 + 24 + 28 + 12, 0);
  refreshCrc(zeroAdvance);
  require(view.begin(zeroAdvance.data(), zeroAdvance.size())
              && view.glyph(0x41, glyph) && glyph.advance == 0,
          "zero advance must remain available for combining glyphs");
}

void everyReadMatchesVirtualVlw() {
  const auto value = fixture();
  View view;
  require(view.begin(value.stored.data(), value.stored.size()), "fixture rejected");
  // Exhaust all small ranges, including metadata-to-pixels and odd nibble
  // offsets, rather than reproducing just one encoder's expected read sizes.
  for (size_t offset = 0; offset <= value.raw.size() + 1; ++offset) {
    for (size_t length = 0; length <= value.raw.size() + 2; ++length) {
      Bytes output(length + 2, 0xAC);
      const size_t available = offset < value.raw.size() ? value.raw.size() - offset : 0;
      const size_t expected = std::min(length, available);
      require(view.read(uint32_t(offset), output.data() + 1, length) == expected,
              "read length differs from the bounded virtual stream");
      require(output.front() == 0xAC && output[expected + 1] == 0xAC,
              "read wrote outside its returned range");
      require(std::equal(output.begin() + 1, output.begin() + 1 + expected,
                         value.raw.begin() + std::min(offset, value.raw.size())),
              "virtual header, metadata or expanded nibble differs");
    }
  }
}

void boundedReadsDoNotWrap() {
  const auto value = fixture();
  View view;
  std::array<uint8_t, 8> output{};
  require(view.read(0, output.data(), output.size()) == 0, "unbound view exposed bytes");
  require(!view.begin(nullptr, value.stored.size()), "null asset accepted");
  require(view.begin(value.stored.data(), value.stored.size()), "fixture rejected");
  require(view.read(0, nullptr, 1) == 0, "null output was dereferenced");
  require(view.read(0, nullptr, 0) == 0, "zero-length null read changed state");
  require(view.read(std::numeric_limits<uint32_t>::max(), output.data(), 8) == 0,
          "overflowing offset wrapped into the stream");
  require(view.read(uint32_t(value.raw.size() - 2), output.data(),
                    std::numeric_limits<size_t>::max()) == 2,
          "huge requested length was not clamped before arithmetic");
  require(output[0] == value.raw[value.raw.size() - 2]
              && output[1] == value.raw.back(), "tail read was incorrect");
}

void oddPixelsUseOneContinuousStream() {
  const auto value = fixture(true);
  View view;
  require(view.begin(value.stored.data(), value.stored.size()), "odd fixture rejected");
  Bytes output(value.raw.size());
  require(view.read(0, output.data(), output.size()) == output.size()
              && output == value.raw, "odd pixel stream or trailing pad was decoded incorrectly");
  // The one-byte transparent space makes A start on a low nibble, and its
  // two-byte bitmap makes the following CJK glyph also start on a low nibble.
  std::array<uint8_t, 3> middle{};
  require(view.read(value.bitmapStart + 3, middle.data(), middle.size()) == 3
              && middle[0] == 51 && middle[1] == 68 && middle[2] == 85,
          "glyph boundaries must not realign packed pixel nibbles");
  require(view.read(uint32_t(value.raw.size()), middle.data(), 1) == 0,
          "unused final low nibble became an extra pixel");
  auto noncanonical = value.stored;
  noncanonical.back() |= 0x0F;
  refreshCrc(noncanonical);
  rejected(noncanonical, "nonzero final unused low nibble");
}

void rejectsMalformedOuterHeader() {
  const auto original = fixture().stored;
  for (size_t length = 0; length < 64; ++length) {
    Bytes shortHeader(original.begin(), original.begin() + length);
    rejected(shortHeader, "short outer header");
  }
  for (const size_t offset : {size_t(0), size_t(5), size_t(6), size_t(7)}) {
    auto value = original;
    value[offset] ^= 1;
    rejected(value, "magic byte mutation");
  }
  for (uint32_t version : {0u, 2u, 0xFFFFFFFFu}) {
    auto value = original;
    little32(value, 8, version);
    rejected(value, "unknown wrapper version");
  }
  for (size_t offset = 32; offset < 64; ++offset) {
    auto value = original;
    value[offset] = 1;
    rejected(value, "nonzero reserved header byte");
  }
}

void rejectsDeclaredSizeAndBoundaryErrors() {
  const auto original = fixture().stored;
  for (const size_t field : {size_t(12), size_t(16), size_t(20), size_t(24)}) {
    for (uint32_t wrong : {0u, 1u, 0xFFFFFFFFu}) {
      auto value = original;
      little32(value, field, wrong);
      rejected(value, "inconsistent or overflowing declared dimension");
    }
  }
  auto extra = original;
  extra.push_back(0);
  rejected(extra, "trailing byte outside declared asset");
  auto truncated = original;
  truncated.pop_back();
  rejected(truncated, "missing packed bitmap byte");
  // A physically complete buffer with a self-consistent outer size still
  // must satisfy the virtual/packed-size relation.
  little32(extra, 12, uint32_t(extra.size()));
  refreshCrc(extra);
  rejected(extra, "extra packed data inconsistent with virtual byte count");
}

void rejectsCrcAndClearsPriorBinding() {
  const auto value = fixture();
  for (const size_t offset : {size_t(28), size_t(64), size_t(100),
                              value.stored.size() - 1}) {
    auto bad = value.stored;
    bad[offset] ^= 1;
    rejected(bad, "CRC mismatch");
  }
  View view;
  require(view.begin(value.stored.data(), value.stored.size()), "fixture rejected");
  auto bad = value.stored;
  bad.back() ^= 1;
  require(!view.begin(bad.data(), bad.size()) && !view.valid(),
          "rejected rebind retained the previous valid view");
  std::array<uint8_t, 1> output{};
  require(view.read(0, output.data(), 1) == 0,
          "rejected rebind still exposes previous bytes");
}

void rejectsMalformedVirtualHeaderAndGlyphs() {
  const auto original = fixture().stored;
  const auto mutation = [&](size_t offset, uint32_t wrong, const char* description) {
    auto value = original;
    big32(value, 64 + offset, wrong);
    refreshCrc(value); // Exercise semantic validation rather than CRC rejection.
    rejected(value, description);
  };
  mutation(0, 0, "empty raw VLW count");
  mutation(0, 4, "different inner and outer glyph counts");
  mutation(4, 10, "unsupported VLW version");
  mutation(8, 0, "zero native font size");
  mutation(8, 23, "native size below strict 24px format");
  mutation(8, 25, "native size above strict 24px format");
  mutation(8, 129, "oversized native font size");
  mutation(16, 64, "ascent exceeding strict v1 range");
  mutation(20, 64, "descent exceeding strict v1 range");
  mutation(16, 60, "combined line height reaching 64 pixels");
  mutation(16, 256, "ascent outside representable line metrics");
  mutation(20, 256, "descent outside representable line metrics");
  mutation(16, 255, "combined line height exceeding supported range");
  auto zeroLineHeight = original;
  big32(zeroLineHeight, 64 + 16, 0);
  big32(zeroLineHeight, 64 + 20, 0);
  refreshCrc(zeroLineHeight);
  rejected(zeroLineHeight, "zero total ascent and descent");
  mutation(24 + 28, 0x20, "duplicate codepoint");
  mutation(24 + 28, 0x10, "unsorted codepoint");
  mutation(24 + 2 * 28, 0xD800, "surrogate codepoint");
  mutation(24 + 2 * 28, 0x10000, "non-BMP codepoint");
  const size_t glyph = 24 + 28;
  mutation(glyph + 4, 0, "zero glyph height");
  mutation(glyph + 4, 65, "glyph height exceeding strict v1 range");
  mutation(glyph + 4, 256, "glyph height exceeding uint8 range");
  mutation(glyph + 8, 0, "zero glyph width");
  mutation(glyph + 8, 65, "glyph width exceeding strict v1 range");
  mutation(glyph + 8, 256, "glyph width exceeding uint8 range");
  mutation(glyph + 12, 65, "advance exceeding strict v1 range");
  mutation(glyph + 12, 256, "advance exceeding uint8 range");
  mutation(glyph + 16, 65, "top bearing exceeding strict v1 range");
  mutation(glyph + 16, uint32_t(-65), "negative top bearing exceeding strict v1 range");
  mutation(glyph + 16, 256, "top bearing exceeding supported range");
  mutation(glyph + 16, uint32_t(-256), "negative top bearing exceeding supported range");
  mutation(glyph + 20, 65, "left bearing exceeding strict v1 range");
  mutation(glyph + 20, uint32_t(-65), "negative left bearing exceeding strict v1 range");
  mutation(glyph + 20, 128, "left bearing exceeding int8 range");
  mutation(glyph + 20, uint32_t(-129), "negative left bearing exceeding int8 range");
  mutation(glyph + 24, 2, "unknown transparency flag");
}

void requiresExactBitmapCoverage() {
  const auto original = fixture().stored;
  for (uint32_t width : {2u, 4u}) {
    auto value = original;
    big32(value, 64 + 24 + 28 + 8, width);
    refreshCrc(value);
    rejected(value, "glyph dimensions do not consume the exact virtual pixel stream");
  }
}
} // namespace

int main() {
  struct Test { const char* name; void (*run)(); };
  const Test tests[] = {
      {"standard CRC32", crcMatchesStandardVector},
      {"valid metrics", validHeaderAndMetrics},
      {"all virtual read ranges", everyReadMatchesVirtualVlw},
      {"bounded reads", boundedReadsDoNotWrap},
      {"odd continuous nibbles", oddPixelsUseOneContinuousStream},
      {"outer header rejection", rejectsMalformedOuterHeader},
      {"size and bounds rejection", rejectsDeclaredSizeAndBoundaryErrors},
      {"CRC and rebind rejection", rejectsCrcAndClearsPriorBinding},
      {"VLW metric rejection", rejectsMalformedVirtualHeaderAndGlyphs},
      {"exact bitmap coverage", requiresExactBitmapCoverage},
  };
  for (const auto& test : tests) {
    try { test.run(); }
    catch (const std::exception& error) {
      std::cerr << test.name << ": " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << "10/10 title font asset tests passed\n";
}
