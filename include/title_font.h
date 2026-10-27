#pragma once

#include <M5GFX.h>
#include <cstdint>

namespace UiFont { struct GlyphMetrics; }

// Complete antialiased title face. Initialization maps the dedicated data
// partition and loads VLW indexes once; drawing performs no heap allocation.
// Use on the UI/render task, like the existing static UiFont faces.
namespace UiTitleFont {
bool begin();
bool loaded();
const lgfx::IFont* font();
bool contains(uint16_t codepoint);
bool glyphMetrics(const lgfx::IFont* selectedFont, uint16_t codepoint,
                  UiFont::GlyphMetrics& output);
uint32_t glyphCount();
uint32_t assetBytes();
} // namespace UiTitleFont
