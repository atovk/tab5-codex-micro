#pragma once

#include <cstddef>
#include <cstdint>

namespace MicroMic {
constexpr uint32_t SampleRate=16000;
using Epoch=uint64_t (*)();
using Sink=size_t (*)(const int16_t* samples,size_t count,uint64_t captureEpoch);

struct Status {
  bool available=false;
  bool active=false;
  bool muted=false;
  bool streaming=false;
  bool monitoring=false;
  uint16_t gain=100;
  uint8_t level=0;  // Logarithmic RMS/peak meters, 0..100 over -60..0 dBFS.
  uint8_t peak=0;
  uint16_t rms=0;
  uint16_t rawPeak=0;
  uint32_t frames=0;
  uint32_t samples=0;
  uint32_t sentSamples=0;
  uint32_t droppedSamples=0;
  uint32_t clippedSamples=0;
  uint32_t readErrors=0;
  uint32_t lastFrameAt=0;
  int32_t lastError=0;
};

// Configure the ES7210 and slave receiver after Speaker.begin(), before USB.begin().
// The sink runs on the capture worker and must copy PCM before returning, without
// waiting for transport capacity. Its return value is accepted int16_t samples.
// Epoch must also return immediately. The sink must conditionally queue against
// captureEpoch so a transport session change cannot race the final worker check.
bool begin(Sink sink,Epoch epoch=nullptr);
void setStreaming(bool active);  // A connected USB/Bluetooth receiver requests PCM.
void setMonitoring(bool active); // User-opened microphone meter; never saves PCM.
void setMuted(bool muted);
void setGain(uint16_t percent);
Status status();
}
