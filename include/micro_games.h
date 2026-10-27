#pragma once

#include <stddef.h>
#include <stdint.h>
#include <M5Unified.h>

// Local-only games. No game action is forwarded to the desktop HID interface.
namespace MicroGames {
struct Rect { int16_t x, y, w, h; };
// game: 0 picker, 1 airplane, 2 blocks, 3 dinosaur.
// state: 0 running, 1 paused, 2 game over. Read-only local diagnostics.
struct Diagnostics { int game; int state; uint32_t score; uint32_t ticks; };

void init();
// Match the surrounding controller; a changed theme redraws content once.
void setDarkTheme(bool dark);
// Input is sampled independently of rendering; this only stores a snapshot.
void touch(int16_t x, int16_t y, bool pressed);
bool render(M5Canvas& canvas, uint32_t now, bool force = false);
// Valid until the next render(). Initial screens and mode changes may cover
// the content area; animation frames contain only coalesced damaged tiles.
const Rect* damage(size_t& count);
Diagnostics diagnostics();
}  // namespace MicroGames
