#include "render_service_policy.h"
#include "deferred_setting.h"
#include <cassert>
#include <cstdio>

int main() {
  RenderService::Gate gate;
  assert(!gate.enter(9000, true)); // Initialization is not a render frame.
  gate.begin(1000);
  assert(!gate.enter(4999));
  assert(gate.enter(5000));
  assert(!gate.enter(15000, true)); // A callback cannot recursively service.
  gate.leave();
  assert(gate.enter(9100));
  gate.leave();
  assert(gate.calls() == 2 && gate.maxGap() == 4100);
  assert(gate.enter(9200, true)); // Service the last short span before end.
  gate.leave(); gate.end();
  assert(!gate.enter(99999, true));
  gate.resetProfile();
  gate.begin(0xFFFFFF00u);
  assert(!gate.enter(100));
  assert(gate.enter(3744));
  gate.leave();
  assert(gate.calls() == 1 && gate.maxGap() == 4000);

  DeferredSetting<int> setting(0);
  setting.observe(1, true, 10);
  assert(!setting.due(1000)); // No flash writes while a finger is held.
  setting.observe(1, false, 1000);
  assert(!setting.due(1599) && setting.due(1600));
  setting.completed(false, 1600);
  assert(!setting.due(2199) && setting.due(2200)); // Retry the same revision.
  setting.completed(true, 2200);
  assert(!setting.due(9999));
  setting.observe(0, false, 3000);
  setting.observe(1, false, 3100); // Returning to saved value cancels the write.
  assert(!setting.due(5000));
  setting.observe(2, false, 0xFFFFFF00u);
  assert(!setting.due(343) && setting.due(344));
  std::puts("render scheduling and deferred persistence passed");
}
