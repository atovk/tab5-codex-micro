#include "render_service.h"
#include "render_service_policy.h"
#include <Arduino.h>

namespace RenderService {
namespace {
Gate gate;
Callback service = nullptr;
void run(bool force) {
  if (!service || !gate.enter(micros(), force)) return;
  service();
  gate.leave();
}
} // namespace
void setCallback(Callback callback) { service = callback; }
void beginFrame() { gate.begin(micros()); }
void checkpoint() { run(false); }
void endFrame() { run(true); gate.end(); }
uint32_t calls() { return gate.calls(); }
uint32_t maxGap() { return gate.maxGap(); }
void resetProfile() { gate.resetProfile(); }
} // namespace RenderService
