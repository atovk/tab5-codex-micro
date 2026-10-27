#include "micro_controls.h"

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

void joystickEvent(const Events& events, float angle, float distance) {
  require(events.size() == 1 && events[0].joystick && !events[0].key,
          "expected one joystick event");
  near(events[0].angle, angle, "joystick angle");
  near(events[0].distance, distance, "joystick distance");
}

std::pair<int, int> ringPoint(double angleDegrees) {
  const double angle = angleDegrees * PI / 180.;
  return {244 + int(std::lround(100 * std::cos(angle))),
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
        auto events = sample(int(std::lround(244 + 96 * std::cos(angle))),
                             int(std::lround(352 + 96 * std::sin(angle))),
                             true, 100 + step * 10);
        for (const auto& event : events) {
          require(event.key && std::strcmp(event.key, direction == 1 ? "ENC_CW" : "ENC_CC") == 0
                      && event.action == 2 && !event.joystick,
                  "rounded circle emitted a wrong event");
          ++count;
        }
      }
      noEvents(sample(340, 352, false, 1000), "rounded circle release");
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
  noEvents(sample(244, 352, true, 110), "crossing ring center");
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
  keyEvent(sample(244, 352, true, 1000), "ENC", 1);
  auto visual = MicroControls::visual();
  require(visual.dialPressed && visual.pressStarted == 1000, "encoder hold start was not retained");
  for (int i = 0; i < 100; ++i)
    noEvents(sample(244, 352, true, 1010 + i * 100), "encoder hold repeated");
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
  noEvents(sample(802, 352, true, 120), "key drag to stick");
  noEvents(ring(0, 130), "key drag to ring");
  keyEvent(sample(1300, 750, false, 140), "AG00", 0);

  MicroControls::reset();
  noEvents(sample(1200, 400, true, 100), "blank capture");
  noEvents(sample(120, 150, true, 110), "blank drag to key");
  noEvents(sample(802, 352, true, 120), "blank drag to stick");
  noEvents(sample(0, 0, false, 130), "blank release");

  MicroControls::reset();
  noEvents(ring(0, 100), "ring capture");
  noEvents(sample(930, 610, true, 110), "ring drag to key");
  noEvents(sample(802, 352, true, 120), "ring drag to stick");
  noEvents(sample(0, 0, false, 130), "ring release after drag");

  MicroControls::reset();
  joystickEvent(sample(802, 352, true, 100), 0, 0);
  auto events = sample(930, 610, true, 130);
  require(events.size() == 1 && events[0].joystick && !events[0].key,
          "stick drag retargeted to a key");
  near(events[0].distance, 1, "stick clamp outside capture");
  joystickEvent(sample(0, 0, false, 140), 0, 0);
}

void joystickCardinalAnglesAndDistance() {
  struct Position { int x, y; float angle; };
  const Position directions[] = {{92,0,0}, {0,92,.25f}, {-92,0,.5f}, {0,-92,.75f}};
  for (const auto& direction : directions) {
    MicroControls::reset();
    joystickEvent(sample(802, 352, true, 100), 0, 0);
    for (int quarter = 1; quarter <= 3; ++quarter) {
      joystickEvent(sample(802 + direction.x * quarter / 4,
                           352 + direction.y * quarter / 4, true, 100 + quarter * 30),
                    direction.angle, quarter / 4.f);
      const auto state = MicroControls::visual();
      near(state.stickX, direction.x * quarter / (4.f * 92), "visual stick X");
      near(state.stickY, direction.y * quarter / (4.f * 92), "visual stick Y");
    }
    joystickEvent(sample(802 + direction.x * 4, 352 + direction.y * 4, true, 260),
                  direction.angle, 1);
    noEvents(sample(802 + direction.x, 352 + direction.y, true, 265),
             "normalized stick endpoint repeated");
    joystickEvent(sample(0, 0, false, 270), 0, 0);
    require(!MicroControls::visual().stickActive, "stick active after release");
  }
}

void joystickDeadzoneAndNoLatchedRepeats() {
  MicroControls::reset();
  joystickEvent(sample(802, 352, true, 100), 0, 0);
  for (int i = 0; i < 100; ++i)
    noEvents(sample(802 + (i % 3) - 1, 352 + (i % 5) - 2, true, 110 + i * 25),
             "neutral deadzone jitter");
  joystickEvent(sample(894, 352, true, 3000), 0, 1);
  for (int i = 0; i < 100; ++i)
    noEvents(sample(894, 352, true, 3010 + i * 25), "stationary stick repeated");
  joystickEvent(sample(802, 352, true, 5600), 0, 0);
  noEvents(sample(802, 352, true, 5700), "neutral stick repeated");
  joystickEvent(sample(894, 352, true, 5800), 0, 1);
  joystickEvent(sample(0, 0, false, 5900), 0, 0);
}

void joystickThresholdAndDirectionCrossings() {
  MicroControls::reset();
  joystickEvent(sample(847, 352, true, 100), 0, 45.f/92);
  // Crossing the activation threshold must bypass both movement and time gates.
  joystickEvent(sample(848, 352, true, 101), 0, .5f);
  noEvents(sample(848, 352, true, 102), "threshold latch repeated");
  joystickEvent(sample(847, 352, true, 103), 0, 45.f/92);
  joystickEvent(sample(848, 352, true, 104), 0, .5f);
  joystickEvent(sample(802, 398, true, 105), .25f, .5f);
  noEvents(sample(802, 398, true, 106), "direction latch repeated");
  joystickEvent(sample(756, 352, true, 107), .5f, .5f);
  joystickEvent(sample(802, 306, true, 108), .75f, .5f);
  joystickEvent(sample(848, 352, true, 109), 0, .5f);
  joystickEvent(sample(0, 0, false, 110), 0, 0);
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
  keyEvent(sample(244, 352, true, 100), "ENC", 1);
  MicroControls::cancel();
  keyEvent(drain(), "ENC", 0);
  require(!MicroControls::visual().dialPressed, "cancel retained encoder press");

  MicroControls::reset();
  joystickEvent(sample(894, 352, true, 100), 0, 1);
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
      {"joystick cardinal angles and distance", joystickCardinalAnglesAndDistance},
      {"joystick deadzone and no repeats", joystickDeadzoneAndNoLatchedRepeats},
      {"joystick threshold and direction crossings", joystickThresholdAndDirectionCrossings},
      {"cancel release and neutral", cancellationReleasesAndClearsPendingTurns},
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
