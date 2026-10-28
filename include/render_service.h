#pragma once

#include <cstdint>

// Cooperative input servicing on the render task. The callback must never
// draw, change display orientation, or dispatch commands that re-enter render.
namespace RenderService {
using Callback = void (*)();
void setCallback(Callback callback);
void beginFrame();
void checkpoint();
void endFrame();
uint32_t calls();
uint32_t maxGap();
void resetProfile();
} // namespace RenderService
