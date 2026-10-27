#pragma once

#include <M5GFX.h>
#include <cstddef>
#include <cstdint>

// Decorative rendering only. The caller owns status semantics, scheduling,
// input priority, and presentation of the returned damage rectangles. Call
// this module from the render task only; its scratch storage is shared.
namespace AgentNeon {

constexpr uint8_t SlotCount = 6;
constexpr size_t MaxDamage = 8;
constexpr int CardWidth = 194;
constexpr int CardHeight = 82;

struct Rect { int32_t x = 0, y = 0, w = 0, h = 0; };
enum class Pattern : uint8_t { Off, Steady, Chase, Breathe, Segments };

struct Style {
  uint32_t rgb = 0;
  uint16_t periodMs = 1800;
  uint8_t strength = 255;
  Pattern pattern = Pattern::Off;
};

// Allocate all six caches in PSRAM and calculate the shared edge geometry once.
// Failure disables effects safely; no drawing function allocates memory.
bool init();

// Call after drawing the complete, clean static card, including its badge.
// Geometry is the unpressed 194x82 card. A pressed/dropped card should stay
// invalid until the caller redraws its unpressed base and captures it again.
// Supports the project's unrotated RGB565 canvas, whose raw words are swapped.
bool capture(uint8_t slot, M5Canvas& canvas, const Rect& card,
             const Style& style);

// Update one card and return nonoverlapping screen-space damage rectangles.
// nowMs is supplied by the caller: repeatedly passing 0 freezes every pattern
// at the same phase, including full-versus-dirty diagnostic rendering.
// A clipped canvas is supported; unseen pixels retain their previous levels.
// Insufficient damage capacity, invalid capture, or incompatible canvas makes
// this a no-op. At most MaxDamage rectangles are returned.
size_t step(uint8_t slot, M5Canvas& canvas, uint32_t nowMs,
            Rect* damage, size_t capacity);

// Invalidation does not repaint. Redraw the clean card before recapturing it.
void invalidate(uint8_t slot);
void invalidateAll();

} // namespace AgentNeon
