#pragma once

#include <stdint.h>

// Display-independent game lifecycle. Callers serialize setActive() and frame().
namespace GameLifecycle {
enum class Phase : uint8_t { Running, Paused, Over };

struct Frame {
  bool active;
  bool discardInput;
  bool redraw;
  Phase phase;
  uint32_t elapsedMs;

  bool canAdvance(bool gameSelected, Phase currentPhase) const {
    return active && gameSelected && currentPhase == Phase::Running;
  }
};

class Policy {
 public:
  bool active() const { return active_; }

  void setActive(bool active) {
    if (active_ == active) return;
    // Keep the departure even if the page is re-entered before another frame.
    if (!active) pausePending_ = true;
    active_ = active;
    changed_ = true;
  }

  Frame frame(uint32_t now, bool gameSelected, Phase phase) {
    const bool pause = pausePending_ && gameSelected && phase == Phase::Running;
    uint32_t elapsed = now - lastTick_;
    if (!active_ || !started_ || changed_) elapsed = 0;
    else if (elapsed > 80) elapsed = 80;
    const Frame result {active_, changed_, !started_ || changed_ || pause,
                        pause ? Phase::Paused : phase, elapsed};
    lastTick_ = now;
    started_ = true;
    changed_ = pausePending_ = false;
    return result;
  }

 private:
  bool active_ = false;
  bool changed_ = false;
  bool pausePending_ = false;
  bool started_ = false;
  uint32_t lastTick_ = 0;
};
}  // namespace GameLifecycle
