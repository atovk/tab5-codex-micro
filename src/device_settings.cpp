#include "device_settings.h"
#include <Arduino.h>
#include <Preferences.h>

namespace DeviceSettings {
namespace {
Preferences storage;
Values current;
bool ready=false, dirty=false, wasTouching=false;
uint32_t changed=0;
uint32_t encode(const Values& value) {
  return 0xA5000000u | uint32_t(value.sound) | uint32_t(value.volume)<<1
      | uint32_t(value.brightness)<<8 | uint32_t(value.english)<<15;
}
}
Values begin() {
  ready=storage.begin("micro-hw",false);
  const uint32_t saved=ready ? storage.getUInt("prefs",0) : 0;
  const uint8_t volume=(saved>>1)&127, brightness=(saved>>8)&127;
  if ((saved&0xFFFF0000u)==0xA5000000u && volume<=100 && brightness>=10 && brightness<=100)
    // Bit 15 was unused by earlier firmware: existing preferences retain
    // Chinese, while a fresh install uses the English default in Values.
    current={bool(saved&1),volume,brightness,bool(saved&(1u<<15))};
  return current;
}
void update(const Values& values) {
  Values next=values;
  next.volume=constrain(next.volume,0,100);
  next.brightness=constrain(next.brightness,10,100);
  if (encode(next)==encode(current)) return;
  current=next; dirty=true; changed=millis();
}
void poll(bool touching) {
  if (touching) { wasTouching=true; return; }
  if (wasTouching) {
    wasTouching=false;
    if (dirty) changed=millis();
  }
  if (dirty && !touching && millis()-changed>=600 && ready) {
    if (storage.putUInt("prefs",encode(current))==sizeof(uint32_t)) dirty=false;
    else changed=millis();
  }
}
bool pending() { return dirty; }
uint8_t backlight(uint8_t percent) { return (constrain(percent,10,100)*255+50)/100; }
}
