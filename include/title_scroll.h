#pragma once

#include <cstdint>

// Pure timing policy. Callers provide monotonic uint32_t milliseconds and own
// rendering, input sampling, and resets after an intentional time regression.
namespace TitleScroll {
inline constexpr uint16_t LineHeight = 25;
inline constexpr uint16_t ViewportHeight = 50;
inline constexpr uint32_t DwellMs = 2200;
inline constexpr uint32_t MoveMs = 1000;
inline constexpr uint32_t StepMs = DwellMs + MoveMs;
inline constexpr uint32_t ResumeHoldMs = 1000;
inline constexpr uint32_t HorizontalPixelMs = 25;  // 40 pixels per second.

inline uint32_t cycleMs(uint8_t lines) {
  return lines <= 2 ? 0 : uint32_t(lines) * StepMs;
}

// The renderer repeats the first two rows after the final row. Offsets can
// therefore advance through the last row and wrap seamlessly back to zero.
inline uint16_t offsetAt(uint32_t elapsed, uint8_t lines) {
  if (lines <= 2) return 0;
  const uint32_t row = (elapsed / StepMs) % lines;
  const uint32_t phase = elapsed % StepMs;
  const uint32_t movement = phase < DwellMs ? 0
      : (phase - DwellMs) * LineHeight / MoveMs;
  return uint16_t(row * LineHeight + movement);
}

// A single line stays still when it fits. Overflow travels to its final pixel
// and back, with a reading pause at both ends and no discontinuous wrap.
inline uint32_t horizontalCycleMs(uint16_t overflow) {
  return overflow ? 2 * (DwellMs + uint32_t(overflow) * HorizontalPixelMs) : 0;
}

inline uint16_t horizontalOffsetAt(uint32_t elapsed, uint16_t overflow) {
  if (!overflow) return 0;
  const uint32_t travel = uint32_t(overflow) * HorizontalPixelMs;
  const uint32_t leg = DwellMs + travel;
  const uint32_t phase = elapsed % (2 * leg);
  const bool returning = phase >= leg;
  const uint32_t within = returning ? phase - leg : phase;
  const uint16_t moved = within < DwellMs ? 0 : uint16_t((within - DwellMs) / HorizontalPixelMs);
  return returning ? uint16_t(overflow - moved) : moved;
}

class Clock {
 public:
  // A default-constructed clock establishes its baseline on first advance.
  // An explicit reset establishes it immediately, including reset(0).
  void reset(uint32_t now = 0) {
    last_ = now;
    elapsed_ = 0;
    holdRemaining_ = 0;
    paused_ = false;
    started_ = true;
  }

  void advance(uint32_t now, bool paused) {
    if (!started_) {
      reset(now);
      paused_ = paused;
      return;
    }
    uint32_t delta = now - last_;  // Ordinary uint32_t timer rollover is valid.
    last_ = now;
    if (paused) {
      paused_ = true;
      holdRemaining_ = 0;
      return;
    }
    if (paused_) {
      paused_ = false;
      // Discard the resume-edge interval. A title that has never advanced
      // already has its initial dwell, so it needs no additional hold.
      holdRemaining_ = elapsed_ ? ResumeHoldMs : 0;
      return;
    }
    if (delta <= holdRemaining_) {
      holdRemaining_ -= delta;
      return;
    }
    delta -= holdRemaining_;
    holdRemaining_ = 0;
    elapsed_ += delta;
  }

  uint32_t elapsed() const { return elapsed_; }
  bool paused() const { return paused_; }

 private:
  uint32_t last_ = 0;
  uint32_t elapsed_ = 0;
  uint32_t holdRemaining_ = 0;
  bool paused_ = false;
  bool started_ = false;
};
} // namespace TitleScroll
