#pragma once
#include <cstdint>

// Persist settled values outside touch processing. Failed writes remain dirty
// and are retried after another quiet interval, without wearing NVS every frame.
template<typename T> class DeferredSetting {
 public:
  explicit DeferredSetting(T initial = T{}) : wanted_(initial), saved_(initial) {}
  void observe(T value, bool touching, uint32_t now) {
    if (wanted_ != value) { wanted_ = value; changed_ = now; }
    if (touching || wasTouching_) changed_ = now;
    wasTouching_ = touching;
  }
  bool due(uint32_t now) const {
    return wanted_ != saved_ && !wasTouching_ && uint32_t(now - changed_) >= 600;
  }
  T value() const { return wanted_; }
  void completed(bool success, uint32_t now) {
    if (success) saved_ = wanted_;
    changed_ = now;
  }
 private:
  T wanted_, saved_;
  uint32_t changed_ = 0;
  bool wasTouching_ = false;
};
