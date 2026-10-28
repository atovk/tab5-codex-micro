#include "micro_controls.h"
#include "control_layout.h"

#include <cmath>
#include <cstring>
#include <exception>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using MicroUI::Event;
constexpr double PI = 3.14159265358979323846;
using Events = std::vector<Event>;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void near(float actual, float expected, const std::string& label, float epsilon = .0001f) {
  require(std::fabs(actual - expected) <= epsilon,
          label + ": expected " + std::to_string(expected) + ", got " + std::to_string(actual));
}

Events drain() {
  Events events;
  Event event;
  while (MicroControls::nextEvent(event)) events.push_back(event);
  require(!event.key && !event.joystick && event.action == 0,
          "an empty queue must clear the output event");
  return events;
}

Events sample(int x, int y, bool down, uint32_t now) {
  MicroControls::sample(x, y, down, now);
  return drain();
}

void noEvents(const Events& events, const std::string& label) {
  require(events.empty(), label + ": unexpected event count " + std::to_string(events.size()));
}

void keyEvent(const Events& events, const char* key, int action) {
  require(events.size() == 1, std::string(key) + ": expected one event, got " + std::to_string(events.size()));
  require(!events[0].joystick && events[0].key && std::strcmp(events[0].key, key) == 0
              && events[0].action == action,
          std::string("wrong key/action for ") + key);
}

void keyPair(const Events& events, const char* key) {
  require(events.size() == 2, std::string(key) + ": expected one balanced press/release pair");
  keyEvent({events[0]}, key, 1);
  keyEvent({events[1]}, key, 0);
}

void joystickEvent(const Events& events, float angle, float distance) {
  require(events.size() == 1 && events[0].joystick && !events[0].key,
          "expected one joystick event");
  near(events[0].angle, angle, "joystick angle");
  near(events[0].distance, distance, "joystick distance");
}

std::pair<int, int> ringPoint(double angleDegrees) {
  const double angle = angleDegrees * PI / 180.;
  return {ControlLayout::DialX + int(std::lround(100 * std::cos(angle))),
          352 + int(std::lround(100 * std::sin(angle)))};
}

Events ring(double degrees, uint32_t now) {
  auto point = ringPoint(degrees);
  return sample(point.first, point.second, true, now);
}

void continuousRingTurns() {
  for (int direction : {1, -1}) {
    MicroControls::reset();
    noEvents(ring(170, 100), "initial ring capture");
    int count = 0;
    for (int step = 1; step <= 288; ++step) {
      auto events = ring(170 + direction * step * 5., 100 + step * 10);
      for (const auto& event : events) {
        require(event.key && std::strcmp(event.key, direction == 1 ? "ENC_CW" : "ENC_CC") == 0
                    && event.action == 2 && !event.joystick,
                "ring rotation emitted a wrong event");
        ++count;
      }
      if (step % 72 == 0)
        require(count == (step / 72) * 24,
                "each complete revolution must produce 24 detents; got " + std::to_string(count));
    }
    require(count == 96, "four complete ring turns must produce 96 detents; got " + std::to_string(count));
    require(MicroControls::visual().detent == direction * 96, "visual detents disagree with events");
    noEvents(sample(0, 0, false, 3100), "ring release");
    require(!MicroControls::visual().dialTurning, "ring remains active after release");
  }
}

void ringRoundedSampleDensities() {
  std::string failures;
  for (int samples : {24, 48, 72}) {
    for (int direction : {1, -1}) {
      MicroControls::reset();
      int count = 0;
      for (int step = 0; step <= samples; ++step) {
        const double angle = direction * 2 * PI * step / samples;
        auto events = sample(int(std::lround(ControlLayout::DialX + 96 * std::cos(angle))),
                             int(std::lround(352 + 96 * std::sin(angle))),
                             true, 100 + step * 10);
        for (const auto& event : events) {
          require(event.key && std::strcmp(event.key, direction == 1 ? "ENC_CW" : "ENC_CC") == 0
                      && event.action == 2 && !event.joystick,
                  "rounded circle emitted a wrong event");
          ++count;
        }
      }
      noEvents(sample((ControlLayout::DialX + 96), 352, false, 1000), "rounded circle release");
      const auto detents = MicroControls::visual().detent;
      if (count != 24 || detents != direction * 24) {
        if (!failures.empty()) failures += "; ";
        failures += std::to_string(samples) + " samples " + (direction == 1 ? "CW" : "CC")
                    + " emitted " + std::to_string(count) + ", detent " + std::to_string(detents);
      }
    }
  }
  require(failures.empty(), "complete radius-96 circles must produce 24 detents: " + failures);
}

void ringJitterAndWrap() {
  MicroControls::reset();
  noEvents(ring(179, 100), "ring capture near angle wrap");
  for (int i = 0; i < 100; ++i)
    noEvents(ring(i % 2 ? 177 : 182, 110 + i * 10), "ring jitter across angle wrap");
  require(MicroControls::visual().detent == 0, "jitter changed detent");
  noEvents(sample(0, 0, false, 1200), "jitter release");
}

void ringCenterAndOutsideReanchor() {
  MicroControls::reset();
  noEvents(ring(0, 100), "ring capture");
  noEvents(sample(ControlLayout::DialX, 352, true, 110), "crossing ring center");
  noEvents(ring(180, 120), "opposite ring reentry");
  noEvents(ring(180, 130), "stationary reentry");
  auto events = ring(215, 140);
  require(events.size() == 2, "35 degree motion after center reanchor should produce two turns");
  for (const auto& event : events)
    require(event.key && std::strcmp(event.key, "ENC_CW") == 0 && event.action == 2,
            "wrong turn after center reanchor");
  noEvents(sample(600, 352, true, 150), "outside ring excursion");
  noEvents(ring(20, 160), "reentry after leaving ring");
  noEvents(ring(20, 170), "stationary outside reentry");
  noEvents(sample(0, 0, false, 180), "reanchored ring release");
}

void ringTeleportIsIgnored() {
  MicroControls::reset();
  noEvents(ring(0, 100), "ring capture");
  noEvents(ring(170, 110), "lost-sample half turn");
  noEvents(ring(170, 120), "stationary after lost sample");
  auto events = ring(205, 130);
  require(events.size() == 2, "rotation should recover after discarded teleport");
}

void allKeysPressHoldRelease() {
  struct Key { int x, y; const char* name; };
  const Key keys[] = {
      {120,150,"AG00"}, {330,150,"AG01"}, {530,150,"AG02"},
      {740,150,"AG03"}, {950,150,"AG04"}, {1150,150,"AG05"},
      {100,610,"ACT06"}, {260,610,"ACT07"}, {420,610,"ACT08"},
      {580,610,"ACT09"}, {750,610,"ACT10"}, {930,610,"ACT11"},
      {1140,610,"ACT12"}};
  for (const auto& key : keys) {
    MicroControls::reset();
    keyEvent(sample(key.x, key.y, true, 100), key.name, 1);
    require(MicroControls::visual().held >= 0, "key capture missing from visual state");
    for (int i = 0; i < 200; ++i)
      noEvents(sample(key.x, key.y, true, 110 + i * 50), "held key repeated");
    keyEvent(sample(key.x, key.y, false, 10120), key.name, 0);
    require(MicroControls::visual().held == -1, "released key still held");
    noEvents(sample(key.x, key.y, false, 10130), "duplicate key release");
  }
}

void encoderPressAndHold() {
  MicroControls::reset();
  keyEvent(sample(ControlLayout::DialX, 352, true, 1000), "ENC", 1);
  auto visual = MicroControls::visual();
  require(visual.dialPressed && visual.pressStarted == 1000, "encoder hold start was not retained");
  for (int i = 0; i < 100; ++i)
    noEvents(sample(ControlLayout::DialX, 352, true, 1010 + i * 100), "encoder hold repeated");
  noEvents(ring(45, 11020), "encoder center capture retargeted to ring");
  keyEvent(sample(800, 352, false, 11030), "ENC", 0);
  visual = MicroControls::visual();
  require(!visual.dialPressed && visual.pressStarted == 0 && visual.held == -1,
          "encoder release left pressed state");
}

void capturesNeverRetarget() {
  MicroControls::reset();
  keyEvent(sample(120, 150, true, 100), "AG00", 1);
  noEvents(sample(930, 610, true, 110), "key drag to another key");
  noEvents(sample(ControlLayout::StickX, 352, true, 120), "key drag to stick");
  noEvents(ring(0, 130), "key drag to ring");
  keyEvent(sample(1300, 750, false, 140), "AG00", 0);

  MicroControls::reset();
  noEvents(sample(1200, 400, true, 100), "blank capture");
  noEvents(sample(120, 150, true, 110), "blank drag to key");
  noEvents(sample(ControlLayout::StickX, 352, true, 120), "blank drag to stick");
  noEvents(sample(0, 0, false, 130), "blank release");

  MicroControls::reset();
  noEvents(ring(0, 100), "ring capture");
  noEvents(sample(930, 610, true, 110), "ring drag to key");
  noEvents(sample(ControlLayout::StickX, 352, true, 120), "ring drag to stick");
  noEvents(sample(0, 0, false, 130), "ring release after drag");

  MicroControls::reset();
  joystickEvent(sample(ControlLayout::StickX, 352, true, 100), 0, 0);
  auto events = sample(930, 610, true, 130);
  require(events.size() == 1 && events[0].joystick && !events[0].key,
          "stick drag retargeted to a key");
  near(events[0].distance, 1, "stick clamp outside capture");
  joystickEvent(sample(0, 0, false, 140), 0, 0);
}

void trackedChatAreaNeverCapturesControls() {
  // The center column is read-only. Moving the joystick must not leave its
  // old hit target behind or allow a drag from a title to capture a control.
  for (int x : {ControlLayout::ChatX, ControlLayout::ChatCenterX, 802,
                ControlLayout::ChatX + ControlLayout::ChatWidth - 1}) {
    for (int y : {ControlLayout::ChatY, ControlLayout::StickY,
                  ControlLayout::ChatY + ControlLayout::ChatHeight - 1}) {
      MicroControls::reset();
      noEvents(sample(x, y, true, 100), "tracked chat touch");
      noEvents(sample(ControlLayout::StickX, ControlLayout::StickY, true, 130),
               "tracked chat drag stays inert");
      noEvents(sample(0, 0, false, 160), "tracked chat release");
    }
  }
}

void joystickCardinalAnglesAndDistance() {
  struct Position { int x, y; float angle; };
  constexpr int r = ControlLayout::StickRadius;
  const Position directions[] = {{r,0,0}, {0,r,.25f}, {-r,0,.5f}, {0,-r,.75f}};
  for (const auto& direction : directions) {
    MicroControls::reset();
    joystickEvent(sample(ControlLayout::StickX, 352, true, 100), 0, 0);
    for (int quarter = 1; quarter <= 3; ++quarter) {
      const int dx = direction.x * quarter / 4, dy = direction.y * quarter / 4;
      joystickEvent(sample(ControlLayout::StickX + dx,
                           352 + dy, true, 100 + quarter * 30),
                    direction.angle, std::hypot(float(dx),float(dy)) / r);
      const auto state = MicroControls::visual();
      near(state.stickX, float(dx) / r, "visual stick X");
      near(state.stickY, float(dy) / r, "visual stick Y");
    }
    joystickEvent(sample(ControlLayout::StickX + direction.x * 4, 352 + direction.y * 4, true, 260),
                  direction.angle, 1);
    noEvents(sample(ControlLayout::StickX + direction.x, 352 + direction.y, true, 265),
             "normalized stick endpoint repeated");
    joystickEvent(sample(0, 0, false, 270), 0, 0);
    require(!MicroControls::visual().stickActive, "stick active after release");
  }
}

void joystickDeadzoneAndNoLatchedRepeats() {
  MicroControls::reset();
  joystickEvent(sample(ControlLayout::StickX, 352, true, 100), 0, 0);
  for (int i = 0; i < 100; ++i)
    noEvents(sample(ControlLayout::StickX + (i % 3) - 1, 352 + (i % 5) - 2, true, 110 + i * 25),
             "neutral deadzone jitter");
  joystickEvent(sample((ControlLayout::StickX + ControlLayout::StickRadius), 352, true, 3000), 0, 1);
  for (int i = 0; i < 100; ++i)
    noEvents(sample((ControlLayout::StickX + ControlLayout::StickRadius), 352, true, 3010 + i * 25), "stationary stick repeated");
  joystickEvent(sample(ControlLayout::StickX, 352, true, 5600), 0, 0);
  noEvents(sample(ControlLayout::StickX, 352, true, 5700), "neutral stick repeated");
  joystickEvent(sample((ControlLayout::StickX + ControlLayout::StickRadius), 352, true, 5800), 0, 1);
  joystickEvent(sample(0, 0, false, 5900), 0, 0);
}

void joystickThresholdAndDirectionCrossings() {
  MicroControls::reset();
  constexpr int threshold = ControlLayout::StickRadius / 2;
  constexpr int below = threshold - 1;
  constexpr float belowDistance = float(below) / ControlLayout::StickRadius;
  joystickEvent(sample(ControlLayout::StickX + below, 352, true, 100), 0, belowDistance);
  // Crossing the activation threshold must bypass both movement and time gates.
  joystickEvent(sample(ControlLayout::StickX + threshold, 352, true, 101), 0, .5f);
  noEvents(sample(ControlLayout::StickX + threshold, 352, true, 102), "threshold latch repeated");
  joystickEvent(sample(ControlLayout::StickX + below, 352, true, 103), 0, belowDistance);
  joystickEvent(sample(ControlLayout::StickX + threshold, 352, true, 104), 0, .5f);
  joystickEvent(sample(ControlLayout::StickX, 352 + threshold, true, 105), .25f, .5f);
  noEvents(sample(ControlLayout::StickX, 352 + threshold, true, 106), "direction latch repeated");
  joystickEvent(sample(ControlLayout::StickX - threshold, 352, true, 107), .5f, .5f);
  joystickEvent(sample(ControlLayout::StickX, 352 - threshold, true, 108), .75f, .5f);
  joystickEvent(sample(ControlLayout::StickX + threshold, 352, true, 109), 0, .5f);
  joystickEvent(sample(0, 0, false, 110), 0, 0);
}

void joystickArrowTargets() {
  using D = MicroControls::StickDirection;
  constexpr int arrow = ControlLayout::StickArrowRadius;
  struct Target { int x, y; float angle; D direction; };
  const Target targets[] = {{arrow,0,0,D::Right}, {0,arrow,.25f,D::Down},
                            {-arrow,0,.5f,D::Left}, {0,-arrow,.75f,D::Up}};
  for (const auto& target : targets) {
    MicroControls::reset();
    const int x = ControlLayout::StickX + target.x, y = ControlLayout::StickY + target.y;
    joystickEvent(sample(x, y, true, 100), target.angle, float(arrow) / ControlLayout::StickRadius);
    const auto visual = MicroControls::visual();
    require(visual.stickActive, "arrow center must capture the joystick");
    require(MicroControls::stickDirection(visual.stickX,visual.stickY) == target.direction,
            "arrow center must activate its matching single direction");
    joystickEvent(sample(x, y, false, 120), 0, 0);
    require(!MicroControls::visual().stickActive, "arrow release must end the gesture");
  }
}

void joystickDirectionClassification() {
  using D = MicroControls::StickDirection;
  require(MicroControls::stickDirection(0,0) == D::Neutral, "center must be neutral");
  require(MicroControls::stickDirection(.499f,0) == D::Neutral, "subthreshold motion must remain unlit");
  require(MicroControls::stickDirection(.5f,0) == D::Right, "threshold motion must activate right");
  require(MicroControls::stickDirection(0,-.5f) == D::Up, "negative threshold must activate up");
  struct Position { int x, y; D direction; };
  const Position diagonals[] = {{60,59,D::Right}, {59,60,D::Down},
      {60,60,D::Down}, {-60,60,D::Left}, {-60,-60,D::Up}, {60,-60,D::Right}};
  for (const auto& diagonal : diagonals) {
    MicroControls::reset();
    auto events = sample(ControlLayout::StickX + diagonal.x,
                         ControlLayout::StickY + diagonal.y, true, 100);
    require(events.size() == 1 && events[0].joystick, "diagonal must send one analog sample");
    const auto visual = MicroControls::visual();
    require(MicroControls::stickDirection(visual.stickX,visual.stickY) == diagonal.direction,
            "diagonal must select exactly one existing cardinal sector");
    const auto wireDirection = static_cast<D>(int(std::floor((events[0].angle + .125f) * 4)) % 4);
    require(wireDirection == diagonal.direction, "visual direction must match the emitted native angle");
    joystickEvent(sample(0, 0, false, 120), 0, 0);
  }
}

void cancellationReleasesAndClearsPendingTurns() {
  MicroControls::reset();
  keyEvent(sample(930, 610, true, 100), "ACT11", 1);
  MicroControls::cancel();
  keyEvent(drain(), "ACT11", 0);
  MicroControls::cancel();
  noEvents(drain(), "duplicate cancel release");
  keyEvent(sample(120, 150, true, 110), "AG00", 1);
  keyEvent(sample(0, 0, false, 120), "AG00", 0);

  MicroControls::reset();
  keyEvent(sample(ControlLayout::DialX, 352, true, 100), "ENC", 1);
  MicroControls::cancel();
  keyEvent(drain(), "ENC", 0);
  require(!MicroControls::visual().dialPressed, "cancel retained encoder press");

  MicroControls::reset();
  joystickEvent(sample((ControlLayout::StickX + ControlLayout::StickRadius), 352, true, 100), 0, 1);
  MicroControls::cancel();
  joystickEvent(drain(), 0, 0);
  require(!MicroControls::visual().stickActive, "cancel retained stick capture");
  MicroControls::cancel();
  noEvents(drain(), "duplicate cancel neutral");

  MicroControls::reset();
  noEvents(ring(0, 100), "ring capture before cancel");
  auto point = ringPoint(60);
  MicroControls::sample(point.first, point.second, true, 110); // leave turns pending
  MicroControls::cancel();
  noEvents(drain(), "cancel must discard pending ring turns");
  require(!MicroControls::visual().dialTurning, "cancel retained ring capture");
}

void deferredActionsSubmitOnlyOnInsideRelease() {
  // The bridge selects the slots by known current mapping, never by their
  // default position. Exercise all slots so remapping keeps this contract.
  const int targets[] = {103,265,427,589,761,931,1140};
  for (int index = 0; index < 7; ++index) {
    MicroControls::reset();
    MicroControls::setReleaseActionMask(1 << index);
    const std::string key = "ACT" + std::string(index < 4 ? "0" : "") + std::to_string(index + 6);
    const int x = targets[index];
    noEvents(sample(x, 617, true, 100), "deferred action initial touch");
    require(MicroControls::visual().held >= 0, "pending action needs pressed feedback");
    for (int elapsed : {20,500,1000,10000})
      noEvents(sample(x, 617, true, 100 + elapsed), "deferred action must not repeat or submit while held");
    // Repeating an unchanged policy must not cancel a legitimate gesture.
    MicroControls::setReleaseActionMask(1 << index);
    keyPair(sample(x, 617, false, 10200), key.c_str());
    require(MicroControls::visual().held == -1, "submitted action left pressed feedback");
    noEvents(sample(x, 617, false, 10210), "duplicate deferred release");
  }
}

void deferredExitCancelsPermanently() {
  for (const auto outside : {std::pair<int,int>{189,617}, {340,617}, {265,541}, {265,692}}) {
    MicroControls::reset();
    MicroControls::setReleaseActionMask(0x46); // known default Approve, Reject, Send
    noEvents(sample(265, 617, true, 100), "approve start");
    noEvents(sample(outside.first, outside.second, true, 110), "approve exit");
    require(MicroControls::visual().held == -1, "exit must clear pending feedback immediately");
    noEvents(sample(265, 617, true, 120), "reentry must not revive canceled action");
    noEvents(sample(427, 617, true, 130), "canceled action cannot retarget reject");
    noEvents(sample(265, 617, false, 140), "canceled action release");
    noEvents(sample(265, 617, true, 150), "a new touch may arm again");
    keyPair(sample(265, 617, false, 160), "ACT07");
  }
}

void deferredOutsideReleaseAndCancellation() {
  // Include direct outside release (no intermediate movement sample) and
  // synthetic page/transport handoff release at 0,0.
  for (const auto outside : {std::pair<int,int>{427,617}, {0,0}}) {
    MicroControls::reset();
    MicroControls::setReleaseActionMask(0x46);
    noEvents(sample(265, 617, true, 100), "deferred start before outside release");
    noEvents(sample(outside.first, outside.second, false, 120), "outside release must cancel");
    require(MicroControls::visual().held == -1, "outside release retained pressed feedback");
  }
  for (int cancelMode = 0; cancelMode < 3; ++cancelMode) {
    MicroControls::reset();
    MicroControls::setReleaseActionMask(0x46);
    noEvents(sample(265, 617, true, 100), "deferred start before policy invalidation");
    if (cancelMode == 0) MicroControls::cancel();
    else if (cancelMode == 1) MicroControls::cancelDeferredAction();
    else MicroControls::setReleaseActionMask(0);
    noEvents(drain(), "cancel/policy invalidation must never submit a deferred action");
    require(MicroControls::visual().held == -1, "cancellation retained pressed feedback");
    noEvents(sample(265, 617, false, 120), "release after policy invalidation");
  }
}

void enabledSafetyPreservesNativeHoldTiming() {
  struct Key { int x, y; const char* name; };
  const Key keys[] = {{ControlLayout::DialX,352,"ENC"}, {761,617,"ACT10"},
      {125,151,"AG00"}, {103,617,"ACT06"}, {589,617,"ACT09"}, {931,617,"ACT11"}};
  for (const auto& key : keys) {
    MicroControls::reset();
    MicroControls::setReleaseActionMask(0x46);
    keyEvent(sample(key.x, key.y, true, 1000), key.name, 1);
    for (int elapsed : {20,499,500,1000,10000}) {
      // Mapping/link freshness changes may cancel only deferred actions, never
      // truncate an encoder, PTT, agent, fast, fork, or custom native hold.
      MicroControls::cancelDeferredAction();
      noEvents(drain(), "deferred cancellation altered a native hold");
      noEvents(sample(key.x, key.y, true, 1000 + elapsed), "native hold repeated");
    }
    if (std::strcmp(key.name, "ENC") == 0)
      require(MicroControls::visual().pressStarted == 1000, "encoder original hold time changed");
    noEvents(sample(0, 0, true, 11100), "native hold canceled by sliding out");
    keyEvent(sample(0, 0, false, 11200), key.name, 0);
  }
  // Learning a single-shot mapping after the native down was sent cannot
  // transform that in-flight press into a delayed second click.
  MicroControls::reset();
  keyEvent(sample(265, 617, true, 100), "ACT07", 1);
  MicroControls::setReleaseActionMask(0x46);
  noEvents(sample(265, 617, true, 10000), "policy change repeated original native down");
  keyEvent(sample(265, 617, false, 10100), "ACT07", 0);
}

void deferredPairHasAtomicQueueCapacity() {
  MicroControls::reset();
  MicroControls::setReleaseActionMask(0x46);
  // Deliberately leave 31 queued native events; a deferred pair cannot fit.
  for (int index = 0; index < 15; ++index) {
    MicroControls::sample(125, 151, true, 100 + index * 20);
    MicroControls::sample(125, 151, false, 110 + index * 20);
  }
  auto point = ringPoint(0);
  MicroControls::sample(point.first, point.second, true, 500);
  point = ringPoint(20);
  MicroControls::sample(point.first, point.second, true, 520);
  MicroControls::sample(0, 0, false, 540);
  MicroControls::sample(265, 617, true, 600);
  MicroControls::sample(265, 617, false, 620);
  const auto events = drain();
  require(events.size() == 31, "deferred pair must not partially fill a full queue");
  for (const auto& event : events)
    require(event.key && std::strcmp(event.key, "ACT07") != 0, "queue overflow emitted partial deferred press");
  noEvents(sample(265, 617, true, 700), "deferred action recovers after queue drain");
  keyPair(sample(265, 617, false, 720), "ACT07");
}

}  // namespace

void expandedAgentTargetsKeepGapsInert() {
  for (int index = 0; index < 6; ++index) {
    const int x = 125 + 206 * index;
    const std::string key = "AG0" + std::to_string(index);
    for (int y : {106, 195}) {
      MicroControls::reset();
      keyEvent(sample(x, y, true, 100), key.c_str(), 1);
      keyEvent(sample(x, y, false, 120), key.c_str(), 0);
    }
    for (int y : {105, 196}) {
      MicroControls::reset();
      noEvents(sample(x, y, true, 100), "agent gap starts inert");
      noEvents(sample(x, 151, true, 120), "agent gap drag never retargets");
      noEvents(sample(x, 151, false, 140), "agent gap release stays inert");
    }
  }
}

int main() {
  const std::vector<std::pair<const char*, std::function<void()>>> tests = {
      {"continuous ring turns", continuousRingTurns},
      {"ring rounded sample densities", ringRoundedSampleDensities},
      {"ring jitter and angle wrap", ringJitterAndWrap},
      {"ring center and outside reanchor", ringCenterAndOutsideReanchor},
      {"ring teleport rejection", ringTeleportIsIgnored},
      {"all AG and ACT keys", allKeysPressHoldRelease},
      {"expanded AG targets and inert gaps", expandedAgentTargetsKeepGapsInert},
      {"encoder press hold release", encoderPressAndHold},
      {"captured controls never retarget", capturesNeverRetarget},
      {"tracked chat and old stick target stay inert", trackedChatAreaNeverCapturesControls},
      {"joystick cardinal angles and distance", joystickCardinalAnglesAndDistance},
      {"joystick deadzone and no repeats", joystickDeadzoneAndNoLatchedRepeats},
      {"joystick threshold and direction crossings", joystickThresholdAndDirectionCrossings},
      {"joystick arrow touch targets", joystickArrowTargets},
      {"joystick shared direction classification", joystickDirectionClassification},
      {"cancel release and neutral", cancellationReleasesAndClearsPendingTurns},
      {"deferred actions submit on inside release", deferredActionsSubmitOnlyOnInsideRelease},
      {"deferred exit permanently cancels", deferredExitCancelsPermanently},
      {"deferred outside release and invalidation", deferredOutsideReleaseAndCancellation},
      {"safety preserves native hold timing", enabledSafetyPreservesNativeHoldTiming},
      {"deferred pair reserves both queue slots", deferredPairHasAtomicQueueCapacity},
  };
  int failures = 0;
  for (const auto& test : tests) {
    try {
      test.second();
      std::cout << "PASS " << test.first << '\n';
    } catch (const std::exception& error) {
      ++failures;
      std::cerr << "FAIL " << test.first << ": " << error.what() << '\n';
    }
  }
  std::cout << tests.size() - failures << '/' << tests.size() << " touch tests passed\n";
  return failures ? 1 : 0;
}
