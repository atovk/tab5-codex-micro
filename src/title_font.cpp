#include "title_font.h"

#include "title_font_asset.h"
#include "ui_font.h"
#include <esp_partition.h>
#include <limits>

namespace UiTitleFont {
namespace {
TitleFontAsset::View asset;

class PackedVlwWrapper final : public lgfx::DataWrapper {
 public:
  void attach(const TitleFontAsset::View* source) { source_ = source; position_ = 0; }
  int read(uint8_t* destination, uint32_t length) override {
    if (!source_) return 0;
    if (length > uint32_t(std::numeric_limits<int>::max()))
      length = uint32_t(std::numeric_limits<int>::max());
    const std::size_t count = source_->read(position_, destination, length);
    position_ += uint32_t(count);
    return int(count);
  }
  void skip(int32_t offset) override {
    if (!source_) return;
    const int64_t wanted = int64_t(position_) + offset;
    position_ = wanted < 0 ? 0 : wanted > source_->rawBytes()
        ? source_->rawBytes() : uint32_t(wanted);
  }
  bool seek(uint32_t offset) override {
    if (!source_ || offset > source_->rawBytes()) return false;
    position_ = offset;
    return true;
  }
  void close() override {}  // Mapping belongs to this persistent module.
  int32_t tell() override { return int32_t(position_); }

 private:
  const TitleFontAsset::View* source_ = nullptr;
  uint32_t position_ = 0;
};

// Declaration order keeps the wrapper and asset alive until VLW destruction.
PackedVlwWrapper wrapper;
lgfx::VLWfont titleFace;
esp_partition_mmap_handle_t mappingHandle = 0;
bool attempted = false;
bool ready = false;
} // namespace

bool begin() {
  if (attempted) return ready;
  attempted = true;
  const esp_partition_t* partition = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "spiffs");
  if (!partition || partition->size < TitleFontAsset::HeaderBytes) return false;

  uint8_t header[TitleFontAsset::HeaderBytes];
  if (esp_partition_read(partition, 0, header, sizeof(header)) != ESP_OK) return false;
  const uint32_t length = TitleFontAsset::little32(header + 12);
  if (length < sizeof(header) || length > partition->size) return false;
  const void* mapping = nullptr;
  if (esp_partition_mmap(partition, 0, length, ESP_PARTITION_MMAP_DATA,
                         &mapping, &mappingHandle) != ESP_OK) return false;
  if (!asset.begin(static_cast<const uint8_t*>(mapping), length)) {
    esp_partition_munmap(mappingHandle);
    mappingHandle = 0;
    return false;
  }
  wrapper.attach(&asset);
  if (!titleFace.loadFont(&wrapper)) {
    // Stock VLW may have allocated only some of its index arrays on failure.
    titleFace.unloadFont();
    wrapper.attach(nullptr);
    asset.begin(nullptr, 0);
    esp_partition_munmap(mappingHandle);
    mappingHandle = 0;
    return false;
  }
  ready = true;
  return true;
}

bool loaded() { return ready; }
const lgfx::IFont* font() { return ready ? &titleFace : UiFont::sans24(); }
bool contains(uint16_t codepoint) {
  TitleFontAsset::Glyph glyph;
  return ready && asset.glyph(codepoint, glyph);
}
bool glyphMetrics(const lgfx::IFont* selectedFont, uint16_t codepoint,
                  UiFont::GlyphMetrics& output) {
  if (!ready || selectedFont != &titleFace) return false;
  TitleFontAsset::Glyph glyph;
  if (!asset.glyph(codepoint, glyph)) return false;
  output.x = glyph.left;
  output.y = int16_t(int32_t(titleFace.maxAscent) - glyph.top);
  output.w = int16_t(glyph.width);
  output.h = int16_t(glyph.height);
  output.advance = int16_t(glyph.advance);
  output.ink = glyph.ink;
  return true;
}
uint32_t glyphCount() { return ready ? asset.glyphCount() : 0; }
uint32_t assetBytes() { return ready ? asset.storedBytes() : 0; }
} // namespace UiTitleFont
