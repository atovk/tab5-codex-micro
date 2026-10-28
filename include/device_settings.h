#pragma once
#include <cstdint>
namespace DeviceSettings {
struct Values {
  bool sound=true;
  uint8_t volume=45, brightness=24;
  bool english=true;
  bool micMuted=false;
  uint16_t micGain=100;
};
Values begin();
void update(const Values& values);
void poll(bool touching);
bool pending();
uint8_t backlight(uint8_t percent);
}
