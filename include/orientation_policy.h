#pragma once

#include <cmath>
#include <cstdint>

// Fixed-landscape orientation, independent of the sensor and display driver.
// Inputs are gravity in g: positive `down` selects rotation 1, negative selects
// rotation 3; `norm` is the complete acceleration-vector magnitude.
namespace OrientationPolicy {

// Adapt raw IMU axes to the policy's logical down component. Portrait poses
// contribute no landscape evidence; invalid axes must cancel any candidate.
inline float landscapeProjection(float ax, float ay) {
  if (!std::isfinite(ax) || !std::isfinite(ay)) return 0.f;
  return std::fabs(ax) > 1.2f * std::fabs(ay) ? -ax : 0.f;
}

class Policy {
 public:
  static constexpr uint32_t kStableMs = 600;
  static constexpr uint32_t kMaxGapMs = 250;

  explicit Policy(uint8_t initial = 1) { reset(initial); }

  uint8_t rotation() const { return rotation_; }

  void reset(uint8_t rotation = 1) {
    rotation_ = rotation == 3 ? 3 : 1;
    candidate_ = rotation_;
    candidateSince_ = lastSample_ = 0;
    haveCandidate_ = haveSample_ = false;
  }

  void reset(uint8_t rotation, uint32_t now) {
    reset(rotation);
    lastSample_ = now;
    haveSample_ = true;
  }

  uint8_t update(uint32_t now, float down, float norm, bool blocked) {
    // Unsigned elapsed time remains valid across the millis() wrap. A gap
    // breaks continuity; this sample may start a fresh candidate, never finish
    // a stale one. Explicit flags make timestamp zero a normal first sample.
    if (haveSample_ && uint32_t(now - lastSample_) > kMaxGapMs)
      haveCandidate_ = false;
    lastSample_ = now;
    haveSample_ = true;

    if (blocked || !std::isfinite(down) || !std::isfinite(norm)
        || std::fabs(down) < .55f || norm < .75f || norm > 1.25f) {
      haveCandidate_ = false;
      return rotation_;
    }

    const uint8_t target = down > 0.f ? 1 : 3;
    if (target == rotation_) {
      haveCandidate_ = false;
    } else if (!haveCandidate_ || candidate_ != target) {
      candidate_ = target;
      candidateSince_ = now;
      haveCandidate_ = true;
    } else if (uint32_t(now - candidateSince_) >= kStableMs) {
      rotation_ = target;
      haveCandidate_ = false;
    }
    return rotation_;
  }

 private:
  uint32_t candidateSince_ = 0, lastSample_ = 0;
  uint8_t rotation_ = 1, candidate_ = 1;
  bool haveCandidate_ = false, haveSample_ = false;
};

}  // namespace OrientationPolicy
