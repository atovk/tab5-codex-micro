#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>

namespace UsbMicrophone {

constexpr uint32_t sampleRate = 16000;
constexpr size_t blockSamples = 160; // 10ms; the producer never waits for USB.

struct Status {
  bool available=false, mounted=false, streaming=false;
  bool muted=false, hostMuted=false;
  int16_t hostVolumeDb=0;
  uint32_t sampleRate=UsbMicrophone::sampleRate, generation=0, queuedBlocks=0;
  uint32_t writtenSamples=0, droppedSamples=0, txPackets=0, txBytes=0;
};

// Register before USB.begin(). Host alt setting 1 alone starts streaming.
bool begin();
// One producer, at most blockSamples per call. Returns accepted sample count.
// PCM is mono signed 16-bit little endian. Never waits for USB or a mutex.
size_t write(const int16_t* samples, size_t count);
// Epoch obtained before ADC capture. Never relabel old PCM as a new session.
size_t write(const int16_t* samples, size_t count, uint32_t expectedGeneration);
void setMuted(bool muted);
Status status();

namespace Detail {
// One ADC producer and one USB worker. Control callbacks invalidate by generation;
// they never reset either queue index or touch the producer's sample storage.
class PcmQueue {
 public:
  struct Block {
    std::array<int16_t,blockSamples> samples{};
    uint32_t generation=0;
    uint16_t count=0;
  };
  static constexpr uint32_t capacity=3;
  bool push(const int16_t* samples,size_t count,uint32_t generation) {
    if (!samples || count==0 || count>blockSamples) return false;
    const uint32_t w=writeAt.load(std::memory_order_relaxed);
    if (w-readAt.load(std::memory_order_acquire)>=capacity) return false;
    auto& block=blocks[w%slots];
    for (size_t i=0;i<count;++i) block.samples[i]=samples[i];
    block.count=count; block.generation=generation;
    writeAt.store(w+1,std::memory_order_release);
    return true;
  }
  bool pop(Block& block) {
    const uint32_t r=readAt.load(std::memory_order_relaxed);
    if (r==writeAt.load(std::memory_order_acquire)) return false;
    block=blocks[r%slots];
    readAt.store(r+1,std::memory_order_release);
    return true;
  }
  uint32_t size() const {
    const uint32_t r=readAt.load(std::memory_order_acquire);
    const uint32_t w=writeAt.load(std::memory_order_acquire);
    const uint32_t n=w-r;
    return n<=capacity ? n : 0;
  }
 private:
  // Power-of-two storage keeps modulo indexing continuous when uint32_t wraps.
  static constexpr uint32_t slots=4;
  std::array<Block,slots> blocks{};
  std::atomic<uint32_t> readAt{0},writeAt{0};
};
} // namespace Detail
} // namespace UsbMicrophone
