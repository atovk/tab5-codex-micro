#include "agent_neon.h"

#include <esp_heap_caps.h>
#include <algorithm>
#include <cmath>
#include <cstring>

namespace AgentNeon {
namespace {

constexpr size_t MaxPixels = 4096;
constexpr unsigned Levels = 8;
constexpr float Pi = 3.14159265358979323846f;
constexpr float Radius = 19.f;
constexpr float Left = 3.f, Top = 2.f, Width = 188.f, Height = 74.f;
constexpr float InnerLeft = Left + Radius;
constexpr float InnerRight = Left + Width - Radius;
constexpr float InnerTop = Top + Radius;
constexpr float InnerBottom = Top + Height - Radius;
constexpr float StraightX = Width - 2 * Radius;
constexpr float StraightY = Height - 2 * Radius;
constexpr float Quarter = Pi * Radius / 2;
constexpr float Perimeter = 2 * StraightX + 2 * StraightY + 4 * Quarter;

// Straight strips and corners are disjoint. Their union covers the entire
// five-pixel light band while leaving the card's title and badge untouched.
constexpr Rect Patches[MaxDamage] = {
  {0, 0, 24, 24}, {24, 0, 146, 8}, {170, 0, 24, 24},
  {185, 24, 9, 34}, {170, 58, 24, 24}, {24, 74, 146, 8},
  {0, 58, 24, 24}, {0, 24, 9, 34},
};

struct Pixel {
  uint16_t offset;
  uint16_t position;
  uint8_t coverage;
  uint8_t patch;
};
struct Cache {
  Rect card;
  Style style;
  uint16_t* colors = nullptr; // level-major, level zero is the clean base.
  uint8_t* previous = nullptr;
  bool valid = false;
};

Pixel pixels[MaxPixels];
uint8_t desired[MaxPixels];
uint8_t breathe[256];
Cache caches[SlotCount];
size_t pixelCount = 0;
bool attempted = false;
bool ready = false;

bool contains(const Rect& rect, int x, int y) {
  return x >= rect.x && y >= rect.y
      && x < int(rect.x) + rect.w && y < int(rect.y) + rect.h;
}

bool compatible(M5Canvas& canvas) {
  return canvas.getRotation() == 0
      && canvas.getColorDepth() == lgfx::rgb565_2Byte
      && canvas.getBuffer() != nullptr;
}

float distanceFromEdge(float x, float y) {
  const float qx = std::fabs(x - (Left + Width / 2)) - StraightX / 2;
  const float qy = std::fabs(y - (Top + Height / 2)) - StraightY / 2;
  const float ox = std::max(qx, 0.f), oy = std::max(qy, 0.f);
  return std::fabs(std::sqrt(ox * ox + oy * oy)
      + std::min(std::max(qx, qy), 0.f) - Radius);
}

// Clockwise arclength from the upper-left tangent. Only initialization uses
// floating-point geometry; all frame-time work uses normalized integers.
float positionOnEdge(float x, float y) {
  if (y < InnerTop) {
    if (x < InnerLeft) {
      float angle = std::atan2(y - InnerTop, x - InnerLeft);
      if (angle < 0) angle += 2 * Pi;
      return 2 * StraightX + 2 * StraightY + 3 * Quarter
          + Radius * (angle - Pi);
    }
    if (x > InnerRight)
      return StraightX + Radius * (std::atan2(y - InnerTop, x - InnerRight) + Pi / 2);
    return x - InnerLeft;
  }
  if (y > InnerBottom) {
    if (x > InnerRight)
      return StraightX + Quarter + StraightY
          + Radius * std::atan2(y - InnerBottom, x - InnerRight);
    if (x < InnerLeft)
      return 2 * StraightX + 2 * Quarter + StraightY
          + Radius * (std::atan2(y - InnerBottom, x - InnerLeft) - Pi / 2);
    return StraightX + 2 * Quarter + StraightY + InnerRight - x;
  }
  if (x > Left + Width / 2)
    return StraightX + Quarter + y - InnerTop;
  return 2 * StraightX + 3 * Quarter + StraightY + InnerBottom - y;
}

uint16_t blend(uint16_t swappedBase, uint32_t rgb, unsigned alpha) {
  const uint16_t base = uint16_t((swappedBase >> 8) | (swappedBase << 8));
  const unsigned r5 = base >> 11, g6 = (base >> 5) & 63, b5 = base & 31;
  const unsigned br = (r5 << 3) | (r5 >> 2);
  const unsigned bg = (g6 << 2) | (g6 >> 4);
  const unsigned bb = (b5 << 3) | (b5 >> 2);
  const unsigned r = (br * (255 - alpha) + ((rgb >> 16) & 255) * alpha + 127) / 255;
  const unsigned g = (bg * (255 - alpha) + ((rgb >> 8) & 255) * alpha + 127) / 255;
  const unsigned b = (bb * (255 - alpha) + (rgb & 255) * alpha + 127) / 255;
  const uint16_t result = uint16_t(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
  return uint16_t((result >> 8) | (result << 8));
}

uint16_t phaseAt(const Style& style, uint32_t nowMs) {
  const uint32_t period = style.periodMs ? style.periodMs : 1800;
  return uint16_t(((nowMs % period) * 65536u) / period);
}

uint8_t levelAt(Pattern pattern, uint16_t position, uint16_t phase) {
  switch (pattern) {
    case Pattern::Steady: return Levels;
    case Pattern::Chase: {
      // An approximately 82px fading tail around the 491px perimeter.
      constexpr uint32_t Tail = 11000;
      const uint16_t behind = uint16_t(phase - position);
      return behind < Tail ? uint8_t(1 + (Tail - 1 - behind) * Levels / Tail) : 0;
    }
    case Pattern::Breathe: return breathe[phase >> 8];
    case Pattern::Segments:
      // Three stationary dashes breathe together; their geometry distinguishes
      // this effect from both the full breathing ring and the travelling tail.
      return uint16_t(uint32_t(position) * 3) < 15000 ? breathe[phase >> 8] : 0;
    default: return 0;
  }
}

} // namespace

bool init() {
  if (attempted) return ready;
  attempted = true;
  for (int y = 0; y < CardHeight; ++y) {
    for (int x = 0; x < CardWidth; ++x) {
      const float distance = distanceFromEdge(x + .5f, y + .5f);
      if (distance >= 2.5f) continue;
      const float opacity = std::min(1.f, (2.5f - distance) / 2.f);
      const unsigned coverage = unsigned(opacity * opacity * 255 + .5f);
      if (!coverage) continue;
      if (pixelCount == MaxPixels) return false;
      // A geometry edit must never silently allow effects over title pixels.
      if (x >= 16 && x < 178 && y >= 23 && y < 73) return false;
      uint8_t patch = 0;
      while (patch < MaxDamage && !contains(Patches[patch], x, y)) ++patch;
      if (patch == MaxDamage) return false;
      const auto position = uint32_t(positionOnEdge(x + .5f, y + .5f)
                                     * (65536.f / Perimeter));
      pixels[pixelCount++] = {uint16_t(y * CardWidth + x), uint16_t(position),
                             uint8_t(coverage), patch};
    }
  }
  if (!pixelCount) return false;
  for (unsigned i = 0; i < 256; ++i) {
    const float wave = .5f - .5f * std::cos(2 * Pi * i / 256.f);
    breathe[i] = uint8_t(1 + unsigned(wave * (Levels - 1) + .5f));
  }
  // Nine RGB565 levels plus one byte of actual previous state per pixel.
  // Each slot starts aligned, even if a future stencil has an odd length.
  const size_t stride = (pixelCount * ((Levels + 1) * sizeof(uint16_t) + 1) + 3) & ~size_t(3);
  auto* memory = static_cast<uint8_t*>(heap_caps_malloc(stride * SlotCount,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!memory) return false;
  for (unsigned i = 0; i < SlotCount; ++i) {
    caches[i].colors = reinterpret_cast<uint16_t*>(memory + i * stride);
    caches[i].previous = memory + i * stride + pixelCount * (Levels + 1) * sizeof(uint16_t);
  }
  ready = true;
  return true;
}

bool capture(uint8_t slot, M5Canvas& canvas, const Rect& card, const Style& style) {
  if (slot >= SlotCount) return false;
  auto& cache = caches[slot];
  cache.valid = false;
  if (!ready || !compatible(canvas) || card.w != CardWidth || card.h != CardHeight
      || card.x < 0 || card.y < 0 || int(card.x) + card.w > canvas.width()
      || int(card.y) + card.h > canvas.height()) return false;
  cache.card = card;
  cache.style = style;
  cache.style.rgb &= 0xFFFFFF;
  std::memset(cache.previous, 0, pixelCount);
  if (!style.strength || style.pattern == Pattern::Off) {
    cache.style.pattern = Pattern::Off;
    cache.valid = true;
    return true;
  }
  const auto* source = static_cast<const uint16_t*>(canvas.getBuffer());
  const int stride = canvas.width();
  for (size_t i = 0; i < pixelCount; ++i) {
    const auto& pixel = pixels[i];
    const int x = card.x + pixel.offset % CardWidth;
    const int y = card.y + pixel.offset / CardWidth;
    const uint16_t base = source[y * stride + x];
    cache.colors[i] = base;
    for (unsigned level = 1; level <= Levels; ++level) {
      const unsigned alpha = (unsigned(pixel.coverage) * style.strength * level
                              + 255 * Levels / 2) / (255 * Levels);
      cache.colors[level * pixelCount + i] = blend(base, cache.style.rgb, alpha);
    }
  }
  cache.valid = true;
  return true;
}

size_t step(uint8_t slot, M5Canvas& canvas, uint32_t nowMs,
            Rect* damage, size_t capacity) {
  if (!ready || slot >= SlotCount || !damage || !capacity || !compatible(canvas)) return 0;
  auto& cache = caches[slot];
  if (!cache.valid || cache.card.x < 0 || cache.card.y < 0
      || int(cache.card.x) + CardWidth > canvas.width()
      || int(cache.card.y) + CardHeight > canvas.height()) return 0;
  if (cache.style.pattern == Pattern::Off) return 0;
  int32_t clipX, clipY, clipW, clipH;
  canvas.getClipRect(&clipX, &clipY, &clipW, &clipH);
  const int32_t right = clipX + clipW, bottom = clipY + clipH;
  const uint16_t phase = phaseAt(cache.style, nowMs);
  uint8_t mask = 0;
  for (size_t i = 0; i < pixelCount; ++i) {
    const auto& pixel = pixels[i];
    const int x = cache.card.x + pixel.offset % CardWidth;
    const int y = cache.card.y + pixel.offset / CardWidth;
    const bool visible = x >= clipX && x < right && y >= clipY && y < bottom;
    desired[i] = visible ? levelAt(cache.style.pattern, pixel.position, phase) : cache.previous[i];
    if (desired[i] != cache.previous[i]) mask |= uint8_t(1u << pixel.patch);
  }
  if (!mask) return 0;
  size_t count = 0;
  for (unsigned patch = 0; patch < MaxDamage; ++patch) count += (mask >> patch) & 1;
  if (count > capacity) return 0;
  auto* target = static_cast<uint16_t*>(canvas.getBuffer());
  const int stride = canvas.width();
  for (size_t i = 0; i < pixelCount; ++i) {
    if (desired[i] == cache.previous[i]) continue;
    const auto& pixel = pixels[i];
    const int x = cache.card.x + pixel.offset % CardWidth;
    const int y = cache.card.y + pixel.offset / CardWidth;
    target[y * stride + x] = cache.colors[size_t(desired[i]) * pixelCount + i];
    cache.previous[i] = desired[i];
  }
  count = 0;
  for (unsigned patch = 0; patch < MaxDamage; ++patch) {
    if (!(mask & (1u << patch))) continue;
    const auto& local = Patches[patch];
    const int x = std::max<int>(cache.card.x + local.x, clipX);
    const int y = std::max<int>(cache.card.y + local.y, clipY);
    const int r = std::min<int>(cache.card.x + local.x + local.w, right);
    const int b = std::min<int>(cache.card.y + local.y + local.h, bottom);
    damage[count++] = {x, y, r - x, b - y};
  }
  return count;
}

void invalidate(uint8_t slot) {
  if (slot < SlotCount) caches[slot].valid = false;
}

void invalidateAll() {
  for (auto& cache : caches) cache.valid = false;
}

} // namespace AgentNeon
