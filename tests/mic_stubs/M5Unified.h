#pragma once

#include <cstdint>

namespace MicTest {
struct SpeakerConfig {
  uint32_t sample_rate=48000;
  int pin_mck=30,pin_bck=27,pin_ws=29,pin_data_out=26;
};
struct Speaker {
  SpeakerConfig config() const;
  bool isRunning() const;
  bool begin();
  void end();
};
struct I2c {
  bool scanID(uint8_t address) const;
  bool writeRegister8(uint8_t address,uint8_t reg,uint8_t value,uint32_t rate);
};
struct Device { MicTest::Speaker Speaker; I2c In_I2C; };
}

extern MicTest::Device M5;
