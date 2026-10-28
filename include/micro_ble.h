#pragma once
#include "micro_link.h"

namespace MicroBle {
// Starts a worker; radio/HCI initialization never runs in the UI task.
void begin(const char* serial);
void poll();
MicroLink::BleStatus status();
void setEnabled(bool enabled);
void startPairing();
void clearBonds();
void disconnect();
// Main-loop metadata consumer. Only TITLES / HOST_CONFIG are accepted.
bool nextCommand(char* destination, size_t capacity, uint32_t& session);
bool acknowledge(const char* line, uint32_t session);
// Synchronous NimBLE acceptance; never waits for the session mutex or an ACK.
bool submit(uint8_t reportId, const uint8_t* data, size_t length, uint32_t expectedSession);
void diagnostics(Print& output);
}
