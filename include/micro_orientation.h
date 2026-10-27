#pragma once
#include <cstdint>

namespace MicroOrientation {
struct Diagnostics {
  bool available=false, blocked=false;
  uint8_t rotation=3;
  float ax=0, ay=0, az=0, gx=0, gy=0, gz=0;
  uint32_t samples=0, failures=0, changes=0, readMaxUs=0, sampledAt=0;
};
void begin(uint8_t rotation=3);
bool poll(bool blocked);
uint8_t rotation();
// The caller must gate this behind input capture and complete touch release.
void setTestRotation(uint8_t rotation);
Diagnostics diagnostics();
}
