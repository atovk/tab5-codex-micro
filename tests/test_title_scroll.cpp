#include "title_scroll.h"

#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void staticTitles() {
  for (uint8_t lines : {uint8_t(0), uint8_t(1), uint8_t(2)}) {
    require(TitleScroll::cycleMs(lines) == 0, "short titles must have no cycle");
    for (uint32_t time : {0u, 2199u, 2700u, 3200u, 999999u, 0xFFFFFFFFu})
      require(TitleScroll::offsetAt(time, lines) == 0,
              "titles of at most two lines must remain stationary");
  }
  require(TitleScroll::LineHeight == 25 && TitleScroll::ViewportHeight == 50,
          "geometry must retain two native-size rows");
}

void dwellAndMovementBoundaries() {
  struct Sample { uint32_t elapsed; uint16_t offset; };
  const Sample samples[] = {
      {0, 0}, {2199, 0}, {2200, 0}, {2239, 0}, {2240, 1},
      {2700, 12}, {3199, 24}, {3200, 25}, {5399, 25},
      {5400, 25}, {5900, 37}, {6399, 49}, {6400, 50},
      {9599, 74}, {9600, 0}, {11840, 1},
  };
  require(TitleScroll::cycleMs(3) == 9600, "three-line cycle is incorrect");
  for (const auto& sample : samples)
    require(TitleScroll::offsetAt(sample.elapsed, 3) == sample.offset,
            "dwell, move, or cycle boundary changed");
}

void longTitlesStayBoundedAndRepeat() {
  for (uint8_t lines : {uint8_t(3), uint8_t(7), uint8_t(63), uint8_t(255)}) {
    const uint32_t cycle = TitleScroll::cycleMs(lines);
    require(cycle == uint32_t(lines) * 3200, "cycle overflowed for a long title");
    require(TitleScroll::offsetAt(cycle - 1, lines) == uint16_t(lines * 25 - 1),
            "last pixel before wrap is incorrect");
    require(TitleScroll::offsetAt(cycle, lines) == 0, "cycle did not wrap to its first row");
    uint16_t previous = 0;
    for (uint32_t time = 0; time < cycle; ++time) {
      const uint16_t offset = TitleScroll::offsetAt(time, lines);
      require(offset < uint32_t(lines) * 25, "offset escaped the repeated row strip");
      require(offset >= previous && offset - previous <= 1,
              "animation skipped backward or jumped more than one pixel");
      previous = offset;
    }
    for (uint32_t time : {0u, 2199u, 2700u, 3199u, cycle - 1})
      require(TitleScroll::offsetAt(time, lines)
                  == TitleScroll::offsetAt(time + cycle * 1000, lines),
              "complete title cycles changed the displayed offset");
  }
}

void startupAndReset() {
  TitleScroll::Clock clock;
  require(clock.elapsed() == 0 && !clock.paused(), "new clock has nonzero state");
  clock.advance(10000, false);
  require(clock.elapsed() == 0, "first timestamp must establish a baseline");
  clock.advance(10400, false);
  require(clock.elapsed() == 400, "active time did not advance");
  clock.reset(50000);
  require(clock.elapsed() == 0 && !clock.paused(), "reset did not clear clock state");
  clock.advance(50100, false);
  require(clock.elapsed() == 100, "explicit reset timestamp was ignored");
  clock.reset();
  clock.advance(25, false);
  require(clock.elapsed() == 25, "reset(0) must establish an immediate baseline");
}

void pauseMidMoveAndResumeHold() {
  TitleScroll::Clock clock;
  clock.reset(0);
  clock.advance(2700, false);
  require(TitleScroll::offsetAt(clock.elapsed(), 4) == 12, "test must pause mid-move");
  clock.advance(2800, true);
  clock.advance(9000, true);
  require(clock.paused() && clock.elapsed() == 2700, "paused time leaked into animation");
  clock.advance(10000, false);
  require(!clock.paused() && clock.elapsed() == 2700,
          "resume edge must discard its interval");
  clock.advance(10999, false);
  require(clock.elapsed() == 2700, "resume hold ended early");
  clock.advance(11000, false);
  require(clock.elapsed() == 2700, "exact hold endpoint must not advance");
  clock.advance(11040, false);
  require(clock.elapsed() == 2740 && TitleScroll::offsetAt(clock.elapsed(), 4) == 13,
          "animation failed to continue from its paused pixel");
}

void resumedIntervalCanCrossHold() {
  TitleScroll::Clock clock;
  clock.reset();
  clock.advance(2000, false);
  clock.advance(2100, true);
  clock.advance(4000, false);
  clock.advance(5500, false);
  require(clock.elapsed() == 2500, "only time beyond the resume hold must advance");
}

void repeatedPauseRestartsOnlyTheHold() {
  TitleScroll::Clock clock;
  clock.reset();
  clock.advance(100, false);
  clock.advance(200, true);
  clock.advance(300, false);
  clock.advance(700, false);
  require(clock.elapsed() == 100, "first partial hold was consumed as animation");
  clock.advance(800, true);
  clock.advance(900, false);
  clock.advance(1899, false);
  require(clock.elapsed() == 100 && !clock.paused(), "repeated pause did not restart its hold");
  clock.advance(1901, false);
  require(clock.elapsed() == 101, "restarted hold accumulated or discarded active time");
  clock.advance(1901, false);
  require(clock.elapsed() == 101, "duplicate timestamps must be harmless");
}

void initialPauseNeedsNoAdditionalHold() {
  TitleScroll::Clock clock;
  clock.advance(500, true);
  clock.advance(2000, true);
  clock.advance(3000, false);
  require(clock.elapsed() == 0 && !clock.paused(), "initial resume edge changed state");
  clock.advance(3100, false);
  require(clock.elapsed() == 100, "an untouched title already has its startup dwell");
}

void unsignedTimerWrap() {
  TitleScroll::Clock clock;
  clock.reset(0xFFFFFFF0u);
  clock.advance(0x10u, false);
  require(clock.elapsed() == 32, "wall timer wrap lost elapsed time");

  clock.reset(0xFFFFFFFFu - 3000);
  clock.advance(0xFFFFFFFFu - 300, false);
  require(clock.elapsed() == 2700, "pre-wrap active interval is incorrect");
  clock.advance(0xFFFFFFFFu - 200, true);
  clock.advance(100, true);
  clock.advance(200, false);
  clock.advance(1200, false);
  require(clock.elapsed() == 2700, "pause or resume hold broke across timer wrap");
  clock.advance(1240, false);
  require(clock.elapsed() == 2740, "post-wrap animation did not resume");
}
} // namespace

int main() {
  struct Test { const char* name; void (*run)(); };
  const Test tests[] = {
      {"stationary titles", staticTitles},
      {"dwell and movement", dwellAndMovementBoundaries},
      {"long title cycles", longTitlesStayBoundedAndRepeat},
      {"startup and reset", startupAndReset},
      {"pause mid-move", pauseMidMoveAndResumeHold},
      {"hold interval crossing", resumedIntervalCanCrossHold},
      {"repeated pauses", repeatedPauseRestartsOnlyTheHold},
      {"initial pause", initialPauseNeedsNoAdditionalHold},
      {"unsigned timer wrap", unsignedTimerWrap},
  };
  for (const auto& test : tests) {
    try { test.run(); }
    catch (const std::exception& error) {
      std::cerr << test.name << ": " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << "9/9 title scroll tests passed\n";
}
