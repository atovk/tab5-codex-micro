#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

// Arduino-free, allocation-free decoder for the mapped Tab5 title-font asset.
// The virtual byte stream is a conventional big-endian VLW font; its bitmap
// bytes are stored as one continuous high-nibble-first stream of 4-bit alpha.
namespace TitleFontAsset {
inline constexpr std::size_t HeaderBytes = 64;
inline constexpr uint32_t Version = 1;

enum class Error : uint8_t {
  None, Input, Magic, Version, Size, Reserved, Checksum,
  VlwHeader, Unicode, Metrics, Bitmap,
};

struct Glyph {
  uint16_t codepoint = 0;
  uint16_t width = 0, height = 0, advance = 0;
  int16_t top = 0, left = 0;
  bool ink = false;
};

inline uint32_t little32(const uint8_t* bytes) {
  return uint32_t(bytes[0]) | (uint32_t(bytes[1]) << 8)
      | (uint32_t(bytes[2]) << 16) | (uint32_t(bytes[3]) << 24);
}
inline uint32_t big32(const uint8_t* bytes) {
  return (uint32_t(bytes[0]) << 24) | (uint32_t(bytes[1]) << 16)
      | (uint32_t(bytes[2]) << 8) | uint32_t(bytes[3]);
}
inline int32_t signedBig32(const uint8_t* bytes) {
  const uint32_t value = big32(bytes);
  return value <= uint32_t(std::numeric_limits<int32_t>::max())
      ? int32_t(value) : -1 - int32_t(~value);
}

// Standard reflected CRC-32/ISO-HDLC, identical to Python zlib.crc32.
// A 16-entry nibble table keeps startup validation fast without allocating.
inline uint32_t crc32(const uint8_t* bytes, std::size_t length) {
  constexpr uint32_t table[16] = {
      0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu,
      0x76DC4190u, 0x6B6B51F4u, 0x4DB26158u, 0x5005713Cu,
      0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
      0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu,
  };
  uint32_t crc = 0xFFFFFFFFu;
  for (std::size_t i = 0; i < length; ++i) {
    crc ^= bytes[i];
    crc = (crc >> 4) ^ table[crc & 15];
    crc = (crc >> 4) ^ table[crc & 15];
  }
  return crc ^ 0xFFFFFFFFu;
}

class View {
 public:
  // length is the exact stored asset size, not the surrounding partition size.
  // Failed validation discards any previously accepted view.
  bool begin(const uint8_t* bytes, std::size_t length) {
    data_ = nullptr;
    stored_ = raw_ = bitmap_ = count_ = 0;
    error_ = Error::Input;
    if (!bytes || length < HeaderBytes) return false;
    constexpr uint8_t magic[8] = {'T', '5', 'V', 'L', 'W', '4', 0, 0};
    if (std::memcmp(bytes, magic, sizeof(magic))) return fail(Error::Magic);
    if (little32(bytes + 8) != Version) return fail(Error::Version);
    const uint32_t stored = little32(bytes + 12);
    const uint32_t raw = little32(bytes + 16);
    const uint32_t bitmap = little32(bytes + 20);
    const uint32_t count = little32(bytes + 24);
    if (stored != length || !count || count > 65535
        || raw > uint32_t(std::numeric_limits<int32_t>::max())
        || bitmap != 24u + count * 28u || raw < bitmap)
      return fail(Error::Size);
    const uint64_t expected = HeaderBytes + uint64_t(bitmap)
        + (uint64_t(raw - bitmap) + 1) / 2;
    if (expected != length) return fail(Error::Size);
    for (std::size_t i = 32; i < HeaderBytes; ++i)
      if (bytes[i]) return fail(Error::Reserved);
    if (crc32(bytes + HeaderBytes, length - HeaderBytes) != little32(bytes + 28))
      return fail(Error::Checksum);

    const uint8_t* payload = bytes + HeaderBytes;
    const uint32_t size = big32(payload + 8);
    const int32_t ascent = signedBig32(payload + 16);
    const int32_t descent = signedBig32(payload + 20);
    if (big32(payload) != count || big32(payload + 4) != 11
        || size != 24 || big32(payload + 12)
        || ascent < 0 || ascent >= 64 || descent < 0 || descent >= 64
        || ascent + descent < 1 || ascent + descent >= 64)
      return fail(Error::VlwHeader);

    uint32_t previous = 0;
    uint64_t pixels = 0;
    for (uint32_t i = 0; i < count; ++i) {
      const uint8_t* entry = payload + 24 + i * 28;
      const uint32_t code = big32(entry);
      if (code > 0xFFFF || (code >= 0xD800 && code <= 0xDFFF)
          || (i && code <= previous)) return fail(Error::Unicode);
      previous = code;
      const int32_t height = signedBig32(entry + 4);
      const int32_t width = signedBig32(entry + 8);
      const int32_t advance = signedBig32(entry + 12);
      const int32_t top = signedBig32(entry + 16);
      const int32_t left = signedBig32(entry + 20);
      const uint32_t flags = big32(entry + 24);
      // Version 1 is the fixed native-24px title face. Bound each glyph to
      // 64x64: stock VLW drawChar uses stack scratch for its decoded pixels.
      if (height < 1 || height > 64 || width < 1 || width > 64
          || advance < 0 || advance > 64 || top < -64 || top > 64
          || left < -64 || left > 64 || flags > 1)
        return fail(Error::Metrics);
      pixels += uint32_t(width) * uint32_t(height);
      if (pixels > uint64_t(raw - bitmap)) return fail(Error::Bitmap);
    }
    if (pixels != uint64_t(raw - bitmap)) return fail(Error::Bitmap);
    if ((pixels & 1) && (payload[bitmap + uint32_t(pixels >> 1)] & 15))
      return fail(Error::Bitmap);

    data_ = payload;
    stored_ = stored;
    raw_ = raw;
    bitmap_ = bitmap;
    count_ = count;
    error_ = Error::None;
    return true;
  }

  bool valid() const { return data_ != nullptr; }
  Error error() const { return error_; }
  uint32_t storedBytes() const { return stored_; }
  uint32_t rawBytes() const { return raw_; }
  uint32_t bitmapStart() const { return bitmap_; }
  uint32_t glyphCount() const { return count_; }

  // Arbitrary bounded virtual reads, including metadata/pixel crossings and
  // odd pixel offsets. A glyph boundary never resets packed-nibble parity.
  std::size_t read(uint32_t offset, uint8_t* destination, std::size_t length) const {
    if (!valid() || !destination || offset >= raw_) return 0;
    const std::size_t available = raw_ - offset;
    if (length > available) length = available;
    const std::size_t result = length;
    if (offset < bitmap_) {
      std::size_t metadata = bitmap_ - offset;
      if (metadata > length) metadata = length;
      std::memcpy(destination, data_ + offset, metadata);
      destination += metadata;
      offset += uint32_t(metadata);
      length -= metadata;
    }
    if (length) {
      uint32_t pixel = offset - bitmap_;
      const uint8_t* packed = data_ + bitmap_;
      for (std::size_t i = 0; i < length; ++i, ++pixel) {
        const uint8_t byte = packed[pixel >> 1];
        const uint8_t alpha = pixel & 1 ? byte & 15 : byte >> 4;
        destination[i] = uint8_t(alpha * 17);
      }
    }
    return result;
  }

  bool glyph(uint16_t codepoint, Glyph& output) const {
    if (!valid()) return false;
    uint32_t lo = 0, hi = count_;
    while (lo < hi) {
      const uint32_t mid = lo + (hi - lo) / 2;
      const uint8_t* entry = data_ + 24 + mid * 28;
      const uint32_t code = big32(entry);
      if (code < codepoint) lo = mid + 1;
      else hi = mid;
    }
    if (lo == count_) return false;
    const uint8_t* entry = data_ + 24 + lo * 28;
    if (big32(entry) != codepoint) return false;
    output.codepoint = codepoint;
    output.height = uint16_t(big32(entry + 4));
    output.width = uint16_t(big32(entry + 8));
    output.advance = uint16_t(big32(entry + 12));
    output.top = int16_t(signedBig32(entry + 16));
    output.left = int16_t(signedBig32(entry + 20));
    output.ink = !(big32(entry + 24) & 1);
    return true;
  }

 private:
  bool fail(Error error) { error_ = error; return false; }
  const uint8_t* data_ = nullptr;
  uint32_t stored_ = 0, raw_ = 0, bitmap_ = 0, count_ = 0;
  Error error_ = Error::Input;
};
} // namespace TitleFontAsset
