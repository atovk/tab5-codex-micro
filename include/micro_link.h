#pragma once
#include "micro_ui.h"
#include <USBCDC.h>
extern USBCDC DebugSerial;
namespace MicroLink {
void begin();
void poll();
void send(const MicroUI::Event &event);
// Diagnostics use the actual serializer while suppressing desktop actions.
void captureInputs(bool enabled);
bool capturingInputs();
bool previewLights(const char *payload);
void clearLightingPreview();
const MicroUI::State &state();
void diagnostics();
void recordFrame(uint32_t microseconds);
void recordInputInterval(uint32_t microseconds);
void resetMetrics();
// Apply an independently sourced, complete six-slot metadata snapshot.
bool applyTitles(const char *payload);
bool applyHostConfig(const char *payload);
// Synthetic state stays in the explicit, capture-only preview model.
bool previewHostState(const char *payload);
}
