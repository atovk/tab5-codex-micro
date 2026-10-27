#pragma once

#include <M5GFX.h>
#include "material_noise.h"
#include <algorithm>
#include <cmath>
#include <cstring>

// RGB888 material gradients, quantized once with screen-anchored blue noise.
// All drawing preserves the caller's clip and allocates no heap memory.
namespace MaterialGradient {
namespace detail {
constexpr uint8_t bayer[4][4] = {
  {0, 8, 2, 10}, {12, 4, 14, 6}, {3, 11, 1, 9}, {15, 7, 13, 5}
};

struct Channels { int32_t r, g, b; };
struct SurfaceSample { Channels color; uint8_t alpha; };

inline Channels levels(uint32_t rgb) {
  // Keep eight fractional bits until the final 5/6/5 quantization. Interpolating
  // ordinary eight-bit RGB first would introduce another set of visible bands.
  return {int32_t(((rgb >> 16) & 255) * (31 * 256) / 255),
          int32_t(((rgb >> 8) & 255) * (63 * 256) / 255),
          int32_t((rgb & 255) * (31 * 256) / 255)};
}

// Keep exact 5/6/5 levels for flat surfaces. Expanding to RGB888 and converting
// back can introduce fractional levels and unwanted noise into a solid color.
inline Channels levels565(uint16_t rgb565) {
  return {int32_t((rgb565 >> 11) << 8), int32_t(((rgb565 >> 5) & 63) << 8),
          int32_t((rgb565 & 31) << 8)};
}

class Gradient {
 public:
  Gradient(uint32_t top, uint32_t bottom, int y, int height)
      : first(levels(top)), last(levels(bottom)), origin(y),
        span(std::max(1, height - 1)) {}
  Gradient(Channels top, Channels bottom, int y, int height)
      : first(top), last(bottom), origin(y), span(std::max(1, height - 1)) {}

  Channels at(int y) const {
    const int offset = std::max(0, std::min(span, y - origin));
    return {first.r + (last.r - first.r) * offset / span,
            first.g + (last.g - first.g) * offset / span,
            first.b + (last.b - first.b) * offset / span};
  }

 private:
  Channels first, last;
  int origin, span;
};

inline uint16_t pixel(const Channels& c, unsigned threshold) {
  // Legacy 0..15 ordered threshold, retained for generated-client compatibility.
  // The centered threshold covers less than one output level: about +/-4 RGB
  // units in red/blue and +/-2 in green, without changing the average material.
  const unsigned offset = threshold * 16 + 8;
  const unsigned r = (c.r + offset) >> 8;
  const unsigned g = (c.g + offset) >> 8;
  const unsigned b = (c.b + offset) >> 8;
  return uint16_t((r << 11) | (g << 5) | b);
}

inline uint8_t thresholdAt(int x, int y) {
  return MaterialNoise::thresholds[((unsigned(y) & 63) << 6) | (unsigned(x) & 63)];
}

inline uint16_t pixelAt(const Channels& c, int x, int y) {
  // Uniform 0..255 thresholds make every fractional level unbiased across the
  // complete tile, without a visible four-pixel grid or runtime random state.
  const unsigned threshold = thresholdAt(x, y);
  return uint16_t((((c.r + threshold) >> 8) << 11)
                | (((c.g + threshold) >> 8) << 5)
                | ((c.b + threshold) >> 8));
}

class Scanlines {
 public:
  explicit Scanlines(M5Canvas& target) : canvas(target) {
    int32_t x, y, w, h;
    canvas.getClipRect(&x, &y, &w, &h);
    left = std::max<int32_t>(0, x);
    top = std::max<int32_t>(0, y);
    right = std::min<int32_t>(canvas.width(), x + w);
    bottom = std::min<int32_t>(canvas.height(), y + h);
    // This is the exact storage format of the project's unrotated 16-bit
    // canvas. Other sprite formats/rotations use typed, clip-aware pushImage.
    direct = canvas.getRotation() == 0
          && canvas.getColorDepth() == lgfx::rgb565_2Byte
          ? static_cast<uint16_t*>(canvas.getBuffer()) : nullptr;
  }

  int firstRow(int y) const { return std::max(y, top); }
  int endRow(int y) const { return std::min(y, bottom); }

  // Only partially covered pixels read the background. The common opaque
  // interior still uses row(), including its repeated 64-pixel fast path.
  void blendSpan(int x, int y, int width, const Channels& source, uint8_t alpha) {
    if (!alpha) return;
    if (alpha == 255) { row(x, y, width, source); return; }
    coverSpan(x, y, width, source, [alpha](float) { return alpha; });
  }

  template<typename Coverage>
  void coverSpan(int x, int y, int width, const Channels& source, Coverage coverage) {
    if (y < top || y >= bottom) return;
    const int end = std::min(right, x + width);
    x = std::max(left, x);
    width = end - x;
    while (width > 0) {
      const int count = std::min(width, CHUNK);
      uint16_t* destination = direct ? direct + y * canvas.width() + x : nullptr;
      if (destination) std::memcpy(line, destination, count * sizeof(line[0]));
      else canvas.readRect(x, y, count, 1, line);
      bool changed = false;
      for (int i = 0; i < count; ++i) {
        const unsigned alpha = coverage(x + i + .5f);
        if (!alpha) continue;
        changed = true;
        const uint16_t raw = line[i].raw;
        const uint16_t background = uint16_t((raw >> 8) | (raw << 8));
        const Channels base {int32_t((background >> 11) << 8),
          int32_t(((background >> 5) & 63) << 8), int32_t((background & 31) << 8)};
        const Channels mixed {
          int32_t((base.r * (255 - alpha) + source.r * alpha + 127) / 255),
          int32_t((base.g * (255 - alpha) + source.g * alpha + 127) / 255),
          int32_t((base.b * (255 - alpha) + source.b * alpha + 127) / 255)};
        const uint16_t rgb565 = pixelAt(mixed, x + i, y);
        line[i].raw = uint16_t((rgb565 >> 8) | (rgb565 << 8));
      }
      if (changed) {
        if (destination) std::memcpy(destination, line, count * sizeof(line[0]));
        else canvas.pushImage(x, y, count, 1, line);
      }
      x += count;
      width -= count;
    }
  }

  // Variable material colors are only needed in narrow bevels. Keep this
  // separate from coverSpan(), so ordinary AA/shadow paths retain their small
  // constant-source loop. Each edge pixel is composited exactly once.
  template<typename Sampler>
  void shadeSpan(int x, int y, int width, Sampler sample) {
    if (y < top || y >= bottom) return;
    const int end = std::min(right, x + width);
    x = std::max(left, x);
    width = end - x;
    while (width > 0) {
      const int count = std::min(width, CHUNK);
      uint16_t* destination = direct ? direct + y * canvas.width() + x : nullptr;
      if (destination) std::memcpy(line, destination, count * sizeof(line[0]));
      else canvas.readRect(x, y, count, 1, line);
      bool changed = false;
      for (int i = 0; i < count; ++i) {
        const SurfaceSample material = sample(x + i + .5f);
        const unsigned alpha = material.alpha;
        if (!alpha) continue;
        changed = true;
        Channels mixed = material.color;
        if (alpha != 255) {
          const uint16_t raw = line[i].raw;
          const uint16_t background = uint16_t((raw >> 8) | (raw << 8));
          const Channels base = levels565(background);
          mixed = {
            int32_t((base.r * (255 - alpha) + mixed.r * alpha + 127) / 255),
            int32_t((base.g * (255 - alpha) + mixed.g * alpha + 127) / 255),
            int32_t((base.b * (255 - alpha) + mixed.b * alpha + 127) / 255)};
        }
        const uint16_t rgb565 = ((mixed.r | mixed.g | mixed.b) & 255) == 0
            ? uint16_t(((mixed.r >> 8) << 11) | ((mixed.g >> 8) << 5) | (mixed.b >> 8))
            : pixelAt(mixed, x + i, y);
        line[i].raw = uint16_t((rgb565 >> 8) | (rgb565 << 8));
      }
      if (changed) {
        if (destination) std::memcpy(destination, line, count * sizeof(line[0]));
        else canvas.pushImage(x, y, count, 1, line);
      }
      x += count;
      width -= count;
    }
  }

  void row(int x, int y, int width, const Channels& channels) {
    if (y < top || y >= bottom) return;
    const int end = std::min(right, x + width);
    x = std::max(left, x);
    width = end - x;
    if (width <= 0) return;

    const int length = std::min(width, CHUNK);
    if (((channels.r | channels.g | channels.b) & 255) == 0) {
      const uint16_t rgb565 = uint16_t(((channels.r >> 8) << 11)
                           | ((channels.g >> 8) << 5) | (channels.b >> 8));
      const lgfx::swap565_t raw(uint16_t((rgb565 >> 8) | (rgb565 << 8)));
      std::fill_n(line, length, raw);
    } else {
      // Short spans only calculate their visible pixels. Longer spans build one
      // 64-pixel pattern and repeat it, including clipped or unaligned starts.
      const int phases = std::min(length, 64);
      for (int i = 0; i < phases; ++i) {
        const uint16_t rgb565 = pixelAt(channels, x + i, y);
        line[i].raw = uint16_t((rgb565 >> 8) | (rgb565 << 8));
      }
      for (int i = phases; i < length; ++i) line[i] = line[i & 63];
    }

    // CHUNK is a multiple of 64, so every chunk keeps the same global phase.
    // The local row avoids slower per-pixel PSRAM writes and drawPixel calls.
    while (width > 0) {
      const int count = std::min(width, CHUNK);
      if (direct) {
        std::memcpy(direct + y * canvas.width() + x, line,
                    count * sizeof(line[0]));
      } else {
        canvas.pushImage(x, y, count, 1, line);
      }
      x += count;
      width -= count;
    }
  }

 private:
  static constexpr int CHUNK = 256;
  M5Canvas& canvas;
  int left, top, right, bottom;
  uint16_t* direct;
  lgfx::swap565_t line[CHUNK];
};

inline float clamp01(float value) { return std::max(0.f, std::min(1.f, value)); }
inline uint8_t coverage(float distance, uint8_t opacity) {
  return uint8_t(clamp01(.5f - distance) * opacity + .5f);
}

struct MetalRim {
  float cx, cy, radius, inverseRadius, inverseBevel;
  Channels face, highlight, shade;
  uint8_t opacity;

  SurfaceSample at(float x, float y) const {
    const float dx = x - cx, dy = y - cy;
    const float distance = std::sqrt(dx * dx + dy * dy) - radius;
    const uint8_t alpha = coverage(distance, opacity);
    if (!alpha) return {face, 0};
    // Smoothly flatten the bevel at its inner edge. A fixed top-left light
    // creates broad highlight/shade arcs without texturing the quiet face.
    const float t = clamp01(1.f + distance * inverseBevel);
    const float band = t * t * (3.f - 2.f * t);
    const float light = (-.6f * dx - .8f * dy) * inverseRadius;
    const unsigned weight = unsigned(clamp01(band * light * light) * 256.f + .5f);
    const Channels& edge = light >= 0.f ? highlight : shade;
    const Channels color {
      int32_t((face.r * (256 - weight) + edge.r * weight + 128) >> 8),
      int32_t((face.g * (256 - weight) + edge.g * weight + 128) >> 8),
      int32_t((face.b * (256 - weight) + edge.b * weight + 128) >> 8)};
    return {color, alpha};
  }
};

struct Span { float left = 0, right = 0; };
inline int firstPixel(float edge) { return int(std::ceil(edge - .5f)); }
inline int endPixel(float edge) { return int(std::floor(edge - .5f)) + 1; }

// The level sets of a rounded rectangle are themselves rounded rectangles.
// Two spans per row isolate its opaque middle from its subpixel edge bands.
struct RoundShape {
  float cx, cy, coreX, coreY, radius, top, bottom;
  RoundShape(float x, float y, float w, float h, float r)
      : cx(x + w * .5f), cy(y + h * .5f),
        radius(std::max(0.f, std::min(r, std::min(w, h) * .5f))),
        top(y), bottom(y + h) {
    coreX = w * .5f - radius;
    coreY = h * .5f - radius;
  }
  bool span(float y, float level, Span& result) const {
    const float qy = std::fabs(y - cy) - coreY;
    const float r = radius + level;
    if (qy > r) return false;
    const float half = coreX + (qy > 0.f
        ? std::sqrt(std::max(0.f, r * r - qy * qy)) : r);
    if (half <= 0.f) return false;
    result = {cx - half, cx + half};
    return true;
  }
  float distance(float x, float y) const {
    const float qx = std::fabs(x - cx) - coreX;
    const float qy = std::fabs(y - cy) - coreY;
    const float ox = std::max(qx, 0.f), oy = std::max(qy, 0.f);
    return std::sqrt(ox * ox + oy * oy) + std::min(std::max(qx, qy), 0.f) - radius;
  }
};

struct Capsule {
  float x1, y1, x2, y2, dx, dy, length, inverseLength2, radius, top, bottom;
  Capsule(float ax, float ay, float bx, float by, float r)
      : x1(ax), y1(ay), x2(bx), y2(by), dx(bx - ax), dy(by - ay), radius(r),
        top(std::min(ay, by) - r), bottom(std::max(ay, by) + r) {
    const float length2 = dx * dx + dy * dy;
    length = std::sqrt(length2);
    inverseLength2 = length2 > 0.f ? 1.f / length2 : 0.f;
  }
  bool span(float y, float level, Span& result) const {
    const float r = radius + level;
    if (r <= 0.f) return false;
    bool found = false;
    auto add = [&](float left, float right) {
      if (left > right) return;
      if (!found) { result = {left, right}; found = true; }
      else { result.left = std::min(result.left, left); result.right = std::max(result.right, right); }
    };
    auto cap = [&](float x, float cy) {
      const float oy = y - cy;
      if (std::fabs(oy) > r) return;
      const float half = std::sqrt(std::max(0.f, r * r - oy * oy));
      add(x - half, x + half);
    };
    cap(x1, y1); cap(x2, y2);
    if (length > 0.f) {
      if (std::fabs(dy) < .0001f) {
        if (std::fabs(y - y1) <= r) add(std::min(x1, x2), std::max(x1, x2));
      } else {
        const float center = x1 + (y - y1) * dx / dy;
        const float half = r * length / std::fabs(dy);
        float left = center - half, right = center + half;
        if (std::fabs(dx) < .0001f) {
          const float dot = (y - y1) * dy;
          if (dot < 0.f || dot > length * length) return found;
        } else {
          float a = x1 - (y - y1) * dy / dx;
          float b = x1 + (length * length - (y - y1) * dy) / dx;
          if (a > b) std::swap(a, b);
          left = std::max(left, a); right = std::min(right, b);
        }
        add(left, right);
      }
    }
    return found;
  }
  float distance(float x, float y) const {
    const float t = clamp01(((x - x1) * dx + (y - y1) * dy) * inverseLength2);
    const float ox = x - (x1 + t * dx), oy = y - (y1 + t * dy);
    return std::sqrt(ox * ox + oy * oy) - radius;
  }
};

template<typename Shape>
inline void fill(M5Canvas& canvas, const Shape& shape, const Gradient& gradient,
                 uint8_t opacity) {
  if (!opacity) return;
  Scanlines rows(canvas);
  const int end = rows.endRow(int(std::ceil(shape.bottom + .5f)));
  for (int y = rows.firstRow(int(std::floor(shape.top - .5f))); y < end; ++y) {
    const float py = y + .5f;
    Span outer, inner;
    if (!shape.span(py, .5f, outer)) continue;
    const int left = firstPixel(outer.left), right = endPixel(outer.right);
    const Channels source = gradient.at(y);
    auto edge = [&](int from, int to) {
      rows.coverSpan(from, y, to - from, source,
        [&](float px) { return coverage(shape.distance(px, py), opacity); });
    };
    if (shape.span(py, -.5f, inner)) {
      const int insideLeft = std::max(left, firstPixel(inner.left));
      const int insideRight = std::min(right, endPixel(inner.right));
      edge(left, insideLeft);
      rows.blendSpan(insideLeft, y, insideRight - insideLeft, source, opacity);
      edge(insideRight, right);
    } else edge(left, right);
  }
}

inline void stroke(M5Canvas& canvas, const RoundShape& shape, float width,
                   uint32_t rgb, uint8_t opacity) {
  if (width <= 0.f || !opacity) return;
  Scanlines rows(canvas);
  const Channels source = levels(rgb);
  const int end = rows.endRow(int(std::ceil(shape.bottom + .5f)));
  for (int y = rows.firstRow(int(std::floor(shape.top - .5f))); y < end; ++y) {
    const float py = y + .5f;
    Span outer, hole;
    if (!shape.span(py, .5f, outer)) continue;
    const int left = firstPixel(outer.left), right = endPixel(outer.right);
    auto edge = [&](int from, int to) {
      rows.coverSpan(from, y, to - from, source, [&](float px) {
        const float d = shape.distance(px, py);
        return uint8_t(std::max(0.f, clamp01(.5f - d) - clamp01(.5f - d - width)) * opacity + .5f);
      });
    };
    if (shape.span(py, -width - .5f, hole)) {
      edge(left, std::max(left, firstPixel(hole.left)));
      edge(std::min(right, endPixel(hole.right)), right);
    } else edge(left, right);
  }
}

inline void shadow(M5Canvas& canvas, const RoundShape& shape, float blur,
                   uint8_t opacity, uint32_t rgb) {
  if (blur <= 0.f || !opacity) return;
  Scanlines rows(canvas);
  const Channels source = levels(rgb);
  const int end = rows.endRow(int(std::ceil(shape.bottom + blur)));
  for (int y = rows.firstRow(int(std::floor(shape.top - blur))); y < end; ++y) {
    const float py = y + .5f;
    Span outer, inner;
    if (!shape.span(py, blur, outer)) continue;
    const int left = firstPixel(outer.left), right = endPixel(outer.right);
    auto edge = [&](int from, int to) {
      rows.coverSpan(from, y, to - from, source, [&](float px) {
        const float t = clamp01(std::max(0.f, shape.distance(px, py)) / blur);
        return uint8_t((1.f - t * t * (3.f - 2.f * t)) * opacity + .5f);
      });
    };
    if (shape.span(py, 0.f, inner)) {
      const int insideLeft = std::max(left, firstPixel(inner.left));
      const int insideRight = std::min(right, endPixel(inner.right));
      edge(left, insideLeft);
      rows.blendSpan(insideLeft, y, insideRight - insideLeft, source, opacity);
      edge(insideRight, right);
    } else edge(left, right);
  }
}
}  // namespace detail

using detail::thresholdAt;
using detail::pixelAt;

// Conventional, unswapped RGB565 input. M5GFX's native flat fill preserves the
// exact color, current clip and optimized PSRAM row writes without any noise.
inline void solid565(M5Canvas& canvas, int x, int y, int w, int h, uint16_t rgb565) {
  if (w > 0 && h > 0) canvas.fillRect(x, y, w, h, rgb565);
}

// Explicit gradient origin/height keeps partial background repairs identical
// to a full redraw. For example, a body spanning y=0..719 uses origin=0,h=720.
inline void rect(M5Canvas& canvas, int x, int y, int w, int h,
                 uint32_t top, uint32_t bottom, int gradientY, int gradientH) {
  if (w <= 0 || h <= 0) return;
  detail::Scanlines rows(canvas);
  detail::Gradient gradient(top, bottom, gradientY, gradientH);
  const int end = rows.endRow(y + h);
  for (int row = rows.firstRow(y); row < end; ++row)
    rows.row(x, row, w, gradient.at(row));
}

inline void rect(M5Canvas& canvas, int x, int y, int w, int h,
                 uint32_t top, uint32_t bottom) {
  rect(canvas, x, y, w, h, top, bottom, y, h);
}

inline void row(M5Canvas& canvas, int x, int y, int w, uint32_t rgb) {
  detail::Scanlines rows(canvas);
  rows.row(x, y, w, detail::levels(rgb));
}

inline void roundRect(M5Canvas& canvas, float x, float y, float w, float h, float radius,
                      uint32_t top, uint32_t bottom, uint8_t opacity = 255) {
  if (w <= 0 || h <= 0) return;
  detail::RoundShape shape(x, y, w, h, radius);
  detail::Gradient gradient(top, bottom, int(std::floor(y)), std::max(1, int(std::ceil(h))));
  detail::fill(canvas, shape, gradient, opacity);
}

// A vertical top-to-bottom metallic gradient, clipped to the circular face.
inline void circle(M5Canvas& canvas, float cx, float cy, float radius,
                   uint32_t top, uint32_t bottom, uint8_t opacity = 255) {
  if (radius <= 0.f) return;
  roundRect(canvas, cx - radius, cy - radius, radius * 2, radius * 2,
            radius, top, bottom, opacity);
}

inline void solidRoundRect565(M5Canvas& canvas, float x, float y, float w, float h,
                              float radius, uint16_t rgb565, uint8_t opacity = 255) {
  if (w <= 0.f || h <= 0.f) return;
  const detail::Channels flat = detail::levels565(rgb565);
  detail::Gradient gradient(flat, flat, 0, 1);
  detail::fill(canvas, detail::RoundShape(x, y, w, h, radius), gradient, opacity);
}

// A quiet, exact-color key face with shading confined to its bottom edge. The
// same shape is filled once: Gradient::at() clamps the upper face to top565.
inline void bevelRoundRect565(M5Canvas& canvas, float x, float y, float w, float h,
                              float radius, uint16_t top565, uint16_t bottom565,
                              float rolloff = 10.f, uint8_t opacity = 255) {
  if (w <= 0.f || h <= 0.f) return;
  if (rolloff <= 0.f || top565 == bottom565) {
    solidRoundRect565(canvas, x, y, w, h, radius, top565, opacity);
    return;
  }
  rolloff = std::min(rolloff, h);
  detail::Gradient gradient(detail::levels565(top565), detail::levels565(bottom565),
      int(std::ceil(y + h - rolloff)), std::max(1, int(std::ceil(rolloff))));
  detail::fill(canvas, detail::RoundShape(x, y, w, h, radius), gradient, opacity);
}

inline void circle565(M5Canvas& canvas, float cx, float cy, float radius,
                      uint16_t rgb565, uint8_t opacity = 255) {
  if (radius <= 0.f) return;
  solidRoundRect565(canvas, cx - radius, cy - radius, radius * 2, radius * 2,
                    radius, rgb565, opacity);
}

// Quiet metal: an exact RGB565 face with lighting confined to an inside bevel.
// Colors use conventional (unswapped) RGB565. The helper adds no outer shadow
// and never writes beyond the circle's existing radius + 0.5px AA silhouette.
// Suggested bevels: 3px for the r106 dial, 1.5px for the r29 moving stick.
inline void metalCircle565(M5Canvas& canvas, float cx, float cy, float radius,
                           uint16_t face565, uint16_t highlight565, uint16_t shade565,
                           float bevel = 2.f, uint8_t opacity = 255) {
  if (radius <= 0.f || !opacity) return;
  if (bevel <= 0.f || (face565 == highlight565 && face565 == shade565)) {
    circle565(canvas, cx, cy, radius, face565, opacity);
    return;
  }
  bevel = std::min(bevel, radius);
  const detail::Channels face = detail::levels565(face565);
  const detail::MetalRim rim {cx, cy, radius, 1.f / radius, 1.f / bevel,
      face, detail::levels565(highlight565), detail::levels565(shade565), opacity};
  const detail::RoundShape shape(cx - radius, cy - radius, radius * 2.f,
                                radius * 2.f, radius);
  detail::Scanlines rows(canvas);
  // The flat span must also exclude partially covered silhouette pixels when
  // a caller requests a bevel narrower than half a pixel.
  const float ringWidth = std::max(.5f, bevel);
  const int end = rows.endRow(int(std::ceil(cy + radius + .5f)));
  for (int y = rows.firstRow(int(std::floor(cy - radius - .5f))); y < end; ++y) {
    const float py = y + .5f;
    detail::Span outer, inner;
    if (!shape.span(py, .5f, outer)) continue;
    const int left = detail::firstPixel(outer.left);
    const int right = detail::endPixel(outer.right);
    const auto sample = [&](float px) { return rim.at(px, py); };
    if (!shape.span(py, -ringWidth, inner)) {
      rows.shadeSpan(left, y, right - left, sample);
      continue;
    }
    const int middleLeft = std::max(left, std::min(right, detail::firstPixel(inner.left)));
    const int middleRight = std::max(middleLeft, std::min(right, detail::endPixel(inner.right)));
    rows.shadeSpan(left, y, middleLeft - left, sample);
    rows.blendSpan(middleLeft, y, middleRight - middleLeft, face, opacity);
    rows.shadeSpan(middleRight, y, right - middleRight, sample);
  }
}

// Strokes are drawn inside the supplied outer silhouette. Their AA fringe is
// at most one pixel; aligned integer box bounds preserve existing dirty bounds.
inline void strokeRoundRect(M5Canvas& canvas, float x, float y, float w, float h,
                            float radius, float width, uint32_t rgb, uint8_t opacity = 255) {
  if (w <= 0.f || h <= 0.f) return;
  detail::stroke(canvas, detail::RoundShape(x, y, w, h, radius), width, rgb, opacity);
}
inline void strokeCircle(M5Canvas& canvas, float cx, float cy, float radius,
                         float width, uint32_t rgb, uint8_t opacity = 255) {
  if (radius <= 0.f) return;
  strokeRoundRect(canvas, cx - radius, cy - radius, radius * 2, radius * 2,
                  radius, width, rgb, opacity);
}

// True round-cap line coverage, including the zero-length (dot) case.
inline void line(M5Canvas& canvas, float x1, float y1, float x2, float y2,
                 float width, uint32_t rgb, uint8_t opacity = 255) {
  if (width <= 0.f) return;
  detail::Capsule shape(x1, y1, x2, y2, width * .5f);
  detail::Gradient gradient(rgb, rgb, 0, 1);
  detail::fill(canvas, shape, gradient, opacity);
}

// Angles follow screen coordinates: zero points right, +90 points down. The
// clockwise sweep has round caps; a sweep of 360 degrees is a complete ring.
inline void arc(M5Canvas& canvas, float cx, float cy, float radius,
                float startDegrees, float endDegrees, float width, uint32_t rgb,
                uint8_t opacity = 255) {
  if (radius <= 0.f || width <= 0.f || !opacity) return;
  constexpr float RAD = .01745329251994329577f;
  float sweep = endDegrees - startDegrees;
  const bool full = std::fabs(sweep) >= 359.999f;
  while (sweep < 0.f) sweep += 360.f;
  while (sweep > 360.f) sweep -= 360.f;
  const float sx = std::cos(startDegrees * RAD), sy = std::sin(startDegrees * RAD);
  const float ex = std::cos((startDegrees + sweep) * RAD), ey = std::sin((startDegrees + sweep) * RAD);
  const float half = width * .5f;
  if (!full && sweep < .0001f) {
    circle(canvas, cx + sx * radius, cy + sy * radius, half, rgb, rgb, opacity);
    return;
  }
  detail::RoundShape outer(cx - radius - half, cy - radius - half,
      (radius + half) * 2, (radius + half) * 2, radius + half);
  detail::Scanlines rows(canvas);
  const detail::Channels source = detail::levels(rgb);
  const int end = rows.endRow(int(std::ceil(outer.bottom + .5f)));
  for (int y = rows.firstRow(int(std::floor(outer.top - .5f))); y < end; ++y) {
    const float py = y + .5f;
    detail::Span span, hole;
    if (!outer.span(py, .5f, span)) continue;
    auto edge = [&](int from, int to) {
      rows.coverSpan(from, y, to - from, source, [&](float px) {
        const float dx = px - cx, dy = py - cy;
        const float crossStart = sx * dy - sy * dx, crossEnd = dx * ey - dy * ex;
        const bool onArc = full || (sweep <= 180.f ? crossStart >= 0.f && crossEnd >= 0.f
                                                                  : crossStart >= 0.f || crossEnd >= 0.f);
        float distance;
        if (onArc) distance = std::fabs(std::sqrt(dx * dx + dy * dy) - radius);
        else {
          const float ax = dx - sx * radius, ay = dy - sy * radius;
          const float bx = dx - ex * radius, by = dy - ey * radius;
          distance = std::sqrt(std::min(ax * ax + ay * ay, bx * bx + by * by));
        }
        return detail::coverage(distance - half, opacity);
      });
    };
    const int left = detail::firstPixel(span.left), right = detail::endPixel(span.right);
    if (outer.span(py, -width - .5f, hole)) {
      edge(left, std::max(left, detail::firstPixel(hole.left)));
      edge(std::min(right, detail::endPixel(hole.right)), right);
    } else edge(left, right);
  }
}

// Draw the shadow before its face. The smooth, finite falloff extends `blur`
// pixels beyond the silhouette; callers include that extent in their damage.
inline void shadowRoundRect(M5Canvas& canvas, float x, float y, float w, float h,
                            float radius, float blur, uint8_t opacity, uint32_t rgb) {
  if (w <= 0.f || h <= 0.f) return;
  detail::shadow(canvas, detail::RoundShape(x, y, w, h, radius), blur, opacity, rgb);
}
inline void shadowCircle(M5Canvas& canvas, float cx, float cy, float radius,
                         float blur, uint8_t opacity, uint32_t rgb) {
  if (radius <= 0.f) return;
  shadowRoundRect(canvas, cx - radius, cy - radius, radius * 2, radius * 2,
                  radius, blur, opacity, rgb);
}
}  // namespace MaterialGradient
