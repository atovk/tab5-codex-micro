#include "orientation_policy.h"

#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
using OrientationPolicy::Policy;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void expect(Policy& policy, uint32_t now, float down, float norm, bool blocked,
            uint8_t rotation, const char* description) {
  const uint8_t actual = policy.update(now, down, norm, blocked);
  require(actual == rotation && policy.rotation() == rotation,
          std::string(description) + " at " + std::to_string(now)
          + ": got " + std::to_string(actual));
  require(actual == 1 || actual == 3, "policy returned a portrait rotation");
}

// A normal sensor stream sampled at 20Hz. The first sample contributes zero
// elapsed time; twelve complete intervals are required before a flip.
void settle(Policy& policy, uint32_t start, float down, uint8_t before,
            uint8_t after, float norm = 1.f) {
  for (uint32_t elapsed = 0; elapsed < 600; elapsed += 50)
    expect(policy, start + elapsed, down, norm, false, before, "early flip");
  expect(policy, start + 600, down, norm, false, after, "settled orientation");
}

void initialAndReset() {
  Policy first;
  require(first.rotation() == 1, "default rotation must be 1");
  Policy inverted(3);
  require(inverted.rotation() == 3, "initial inverted rotation was lost");
  for (uint8_t value : {0, 2, 4, 255}) {
    Policy policy(value);
    require(policy.rotation() == 1, "invalid initial rotation must normalize to 1");
    policy.reset(3);
    require(policy.rotation() == 3, "reset did not select rotation 3");
    policy.reset(value);
    require(policy.rotation() == 1, "invalid reset rotation must normalize to 1");
  }
}

void firstSampleAtZeroAndExactTimeBoundary() {
  Policy policy;
  expect(policy, 0, -1, 1, false, 1, "timestamp zero starts a candidate");
  expect(policy, 200, -1, 1, false, 1, "200ms is too early");
  expect(policy, 400, -1, 1, false, 1, "400ms is too early");
  expect(policy, 599, -1, 1, false, 1, "599ms is too early");
  expect(policy, 600, -1, 1, false, 3, "600ms should settle");
}

void bidirectionalAndRotationStability() {
  Policy policy;
  settle(policy, 0, -1, 1, 3);
  for (uint32_t now = 650; now <= 2650; now += 50)
    expect(policy, now, -.8f, 1, false, 3, "stable rotation must not oscillate");
  settle(policy, 2700, 1, 3, 1);
  for (uint32_t now = 3350; now <= 4000; now += 50)
    expect(policy, now, .7f, 1, false, 1, "returned rotation must stay stable");
}

void exactProjectionAndNormBoundaries() {
  Policy negative;
  settle(negative, 0, -.55f, 1, 3, .75f);
  Policy positive(3);
  settle(positive, 0, .55f, 3, 1, 1.25f);
}

void invalidSamplesBreakContinuity() {
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float inf = std::numeric_limits<float>::infinity();
  struct Invalid { float down, norm; };
  const Invalid cases[] = {
    {0, 1}, {-.1f, 1}, {-std::nextafter(.55f, 0.f), 1},
    {-1, std::nextafter(.75f, 0.f)}, {-1, std::nextafter(1.25f, 2.f)},
    {-1, 0}, {-1, -1}, {-1, 2},
    {nan, 1}, {inf, 1}, {-inf, 1}, {-1, nan}, {-1, inf}, {-1, -inf},
  };
  for (const auto& invalid : cases) {
    Policy policy;
    for (uint32_t now = 0; now <= 550; now += 50)
      expect(policy, now, -1, 1, false, 1, "pre-interruption candidate");
    expect(policy, 600, invalid.down, invalid.norm, false, 1,
           "invalid sample at the deadline must cancel");
    settle(policy, 650, -1, 1, 3);
  }
}

void flatPortraitAndShakingHold() {
  Policy policy(3);
  for (uint32_t now = 0; now <= 3000; now += 50) {
    const float down = now % 100 ? .3f : .54f;
    expect(policy, now, down, 1, false, 3, "flat/portrait must hold landscape");
  }
  for (uint32_t now = 3050; now <= 6050; now += 50)
    expect(policy, now, 1, now % 100 ? 1.4f : .6f, false, 3,
           "motion outside the gravity range must not accumulate");
  settle(policy, 6100, 1, 3, 1);
}

void alternatingSignsDoNotAccumulate() {
  Policy policy;
  for (uint32_t index = 0; index < 100; ++index)
    expect(policy, index * 50, index % 2 ? 1 : -1, 1, false, 1,
           "alternating signs must not accumulate opposite orientation time");
  settle(policy, 5000, -1, 1, 3);
}

void returnToCurrentOrientationCancels() {
  Policy policy;
  for (uint32_t now = 0; now <= 550; now += 50)
    expect(policy, now, -1, 1, false, 1, "pending reverse orientation");
  expect(policy, 600, 1, 1, false, 1, "current orientation cancels reverse candidate");
  settle(policy, 650, -1, 1, 3);
}

void gestureBlockRequiresFullFreshInterval() {
  Policy policy;
  for (uint32_t now = 0; now <= 550; now += 50)
    expect(policy, now, -1, 1, false, 1, "candidate before gesture");
  for (uint32_t now = 600; now <= 1200; now += 50)
    expect(policy, now, -1, 1, true, 1, "gesture blocks rotation even at deadline");
  settle(policy, 1250, -1, 1, 3);
  expect(policy, 1900, 1, 1, true, 3, "blocking also holds inverted orientation");
  settle(policy, 1950, 1, 3, 1);
}

void sampleGapBoundaryAndStaleCandidate() {
  Policy continuous;
  for (uint32_t now : {0u, 250u, 500u})
    expect(continuous, now, -1, 1, false, 1, "250ms gap remains continuous");
  expect(continuous, 600, -1, 1, false, 3, "exact max gap should settle");

  Policy interrupted;
  for (uint32_t now : {0u, 200u, 400u})
    expect(interrupted, now, -1, 1, false, 1, "candidate before gap");
  settle(interrupted, 651, -1, 1, 3);  // 251ms gap restarts at 651.

  Policy oneLateSample;
  expect(oneLateSample, 0, -1, 1, false, 1, "initial sample");
  settle(oneLateSample, 600, -1, 1, 3);  // A single late sample must not flip.
}

void duplicateTimestampsDoNotAdvanceTime() {
  Policy policy;
  for (unsigned i = 0; i < 10000; ++i)
    expect(policy, 0, -1, 1, false, 1, "duplicate timestamps cannot settle");
  for (uint32_t now : {200u, 400u, 599u})
    expect(policy, now, -1, 1, false, 1, "only wall time may settle");
  expect(policy, 600, -1, 1, false, 3, "continuous wall time should settle");
}

void unsignedTimestampWrap() {
  const uint32_t start = std::numeric_limits<uint32_t>::max() - 200;
  Policy policy;
  settle(policy, start, -1, 1, 3);
  settle(policy, start + uint32_t(650), 1, 3, 1);

  Policy interrupted;
  expect(interrupted, start, -1, 1, false, 1, "candidate before wrap gap");
  settle(interrupted, start + uint32_t(251), -1, 1, 3);
}

void resetDiscardsCandidateAndClock() {
  Policy policy;
  for (uint32_t now = 100; now <= 650; now += 50)
    expect(policy, now, -1, 1, false, 1, "candidate before reset");
  policy.reset(1);
  settle(policy, 0, -1, 1, 3);  // New clock may start at zero.
  policy.reset(3, 0);
  settle(policy, 0, 1, 3, 1);
  policy.reset(1, 1000);
  // Keep every gap below 250ms so gap invalidation cannot mask an incorrect
  // implementation that counted reset(now) as the first gravity sample.
  for (uint32_t now : {1200u, 1400u, 1600u, 1799u})
    expect(policy, now, -1, 1, false, 1, "reset time must not count as gravity evidence");
  expect(policy, 1800, -1, 1, false, 3, "600ms after the first post-reset sample");
}

void backwardTimestampCannotFinishCandidate() {
  Policy policy;
  for (uint32_t now : {100u, 300u, 500u})
    expect(policy, now, -1, 1, false, 1, "candidate before backward timestamp");
  settle(policy, 50, -1, 1, 3);  // Unsigned large gap safely restarts continuity.
}

void rawAxisAdapterProjection() {
  using OrientationPolicy::landscapeProjection;
  Policy negativeRaw(3);
  settle(negativeRaw, 0, landscapeProjection(-1, 0), 3, 1);
  Policy positiveRaw(1);
  settle(positiveRaw, 0, landscapeProjection(1, 0), 1, 3);

  // A dominant portrait axis supplies zero evidence. A weak raw X can pass
  // dominance but must still fail the policy's independent confidence gate.
  for (uint8_t initial : {1, 3}) {
    Policy held(initial);
    for (uint32_t now = 0; now <= 1500; now += 50)
      expect(held, now, landscapeProjection(.3f, 1), 1, false, initial,
             "portrait raw axes must hold rotation");
    for (uint32_t now = 1550; now <= 3050; now += 50)
      expect(held, now, landscapeProjection(initial == 1 ? .3f : -.3f, 0),
             1, false, initial, "weak landscape projection must not settle");
  }

  const float edge = .6f;  // Exactly 1.2f * .5f in the adapter's arithmetic.
  require(landscapeProjection(edge, .5f) == 0,
          "exact 1.2 dominance boundary must not count as landscape");
  require(landscapeProjection(-edge, -.5f) == 0,
          "dominance boundary must be symmetric across axis signs");
  const float stronger = std::nextafter(edge, 1.f);
  require(landscapeProjection(stronger, .5f) == -stronger,
          "first float above dominance boundary must preserve negated raw X");

  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float interruptions[][2] = {{edge, .5f}, {-edge, -.5f}, {nan, 0},
                                     {1, nan}, {nan, nan}};
  for (const auto& raw : interruptions) {
    Policy interrupted(3);
    for (uint32_t now = 0; now <= 550; now += 50)
      expect(interrupted, now, landscapeProjection(-1, 0), 1, false, 3,
             "raw-axis candidate before interruption");
    expect(interrupted, 600, landscapeProjection(raw[0], raw[1]), 1, false, 3,
           "ambiguous or invalid raw axes must cancel the candidate");
    settle(interrupted, 650, landscapeProjection(-1, 0), 3, 1);
  }
}
}  // namespace

int main() {
  struct Test { const char* name; void (*run)(); };
  const Test tests[] = {
    {"initial/reset", initialAndReset},
    {"zero time / 599-600ms", firstSampleAtZeroAndExactTimeBoundary},
    {"bidirectional stability", bidirectionalAndRotationStability},
    {"inclusive thresholds", exactProjectionAndNormBoundaries},
    {"invalid continuity", invalidSamplesBreakContinuity},
    {"flat/portrait/shaking", flatPortraitAndShakingHold},
    {"alternating signs", alternatingSignsDoNotAccumulate},
    {"return to current", returnToCurrentOrientationCancels},
    {"gesture block", gestureBlockRequiresFullFreshInterval},
    {"250/251ms gap", sampleGapBoundaryAndStaleCandidate},
    {"duplicate timestamps", duplicateTimestampsDoNotAdvanceTime},
    {"uint32 wrap", unsignedTimestampWrap},
    {"reset continuity", resetDiscardsCandidateAndClock},
    {"backward timestamp", backwardTimestampCannotFinishCandidate},
    {"raw-axis projection", rawAxisAdapterProjection},
  };
  for (const auto& test : tests) {
    try { test.run(); }
    catch (const std::exception& error) {
      std::cerr << test.name << ": " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << "15/15 orientation policy tests passed\n";
  return 0;
}
