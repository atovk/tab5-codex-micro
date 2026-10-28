#pragma once

#include <cstdint>

namespace RenderService {
// All timestamps use subtraction so the ~71-minute micros() wrap is harmless.
class Gate {
 public:
  static constexpr uint32_t IntervalUs = 4000;
  void begin(uint32_t now) { active_ = true; last_ = now; }
  void end() { active_ = false; }
  bool enter(uint32_t now, bool force = false) {
    if (!active_ || busy_ || (!force && uint32_t(now - last_) < IntervalUs)) return false;
    const uint32_t gap = now - last_;
    if (gap > maximum_) maximum_ = gap;
    last_ = now; busy_ = true; ++calls_;
    return true;
  }
  void leave() { busy_ = false; }
  uint32_t calls() const { return calls_; }
  uint32_t maxGap() const { return maximum_; }
  void resetProfile() { calls_ = maximum_ = 0; }
 private:
  uint32_t last_ = 0, maximum_ = 0, calls_ = 0;
  bool active_ = false, busy_ = false;
};
} // namespace RenderService
