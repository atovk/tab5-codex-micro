#include "audio_probe.h"

#include <atomic>
#include <driver/i2s_std.h>
#include <esp_err.h>

namespace {
// Tab5's built-in speaker is I2S0 TX. Never count microphone/other-port traffic
// as speaker evidence. All counters are uint32_t and may wrap after long runs.
std::atomic<i2s_chan_handle_t> speaker{nullptr};
std::atomic<uint32_t> enableCalls{0}, enableErrors{0}, writeCalls{0}, writeErrors{0};
std::atomic<uint32_t> writeTimeouts{0}, shortWrites{0}, requestedBytes{0}, writtenBytes{0};
std::atomic<uint32_t> nonblockingRetries{0}, blockingErrors{0}, blockingShortWrites{0};
std::atomic<uint32_t> nonzeroSamples{0}, peak{0};
std::atomic<int32_t> lastEnableResult{0}, lastWriteResult{0};
std::atomic<int32_t> firstWriteError{0}, lastWriteError{0};
std::atomic<uint32_t> otherEnableCalls{0}, otherWriteCalls{0};

bool isSpeaker(i2s_chan_handle_t handle) {
  if (handle && handle==speaker.load(std::memory_order_relaxed)) return true;
  i2s_chan_info_t info{};
  if (handle && i2s_channel_get_info(handle,&info)==ESP_OK
      && info.id==I2S_NUM_0 && info.dir==I2S_DIR_TX) {
    speaker.store(handle,std::memory_order_relaxed);
    return true;
  }
  return false;
}
void recordSamples(const void* source,size_t size) {
  if (!source) return;
  // M5Unified sends signed, little-endian 16-bit PCM on this channel.
  // Inspect only bytes the real driver accepted, without alignment assumptions.
  const auto* data=static_cast<const uint8_t*>(source);
  uint32_t nonzero=0, maximum=0;
  for (size_t i=0;i+1<size;i+=2) {
    const int32_t sample=static_cast<int16_t>(uint16_t(data[i]) | (uint16_t(data[i+1])<<8));
    const uint32_t magnitude=sample<0 ? -sample : sample;
    if (magnitude) ++nonzero;
    if (magnitude>maximum) maximum=magnitude;
  }
  nonzeroSamples.fetch_add(nonzero,std::memory_order_relaxed);
  uint32_t previous=peak.load(std::memory_order_relaxed);
  while (previous<maximum && !peak.compare_exchange_weak(previous,maximum,std::memory_order_relaxed)) {}
}
}  // namespace

extern "C" esp_err_t __real_i2s_channel_enable(i2s_chan_handle_t handle);
extern "C" esp_err_t __real_i2s_channel_write(i2s_chan_handle_t handle,
    const void* src,size_t size,size_t* bytes_written,uint32_t timeout_ms);

extern "C" esp_err_t __wrap_i2s_channel_enable(i2s_chan_handle_t handle) {
  const bool tracked=isSpeaker(handle);
  const esp_err_t result=__real_i2s_channel_enable(handle);
  if (tracked) {
    enableCalls.fetch_add(1,std::memory_order_relaxed);
    lastEnableResult.store(result,std::memory_order_relaxed);
    if (result!=ESP_OK) enableErrors.fetch_add(1,std::memory_order_relaxed);
  } else otherEnableCalls.fetch_add(1,std::memory_order_relaxed);
  return result;
}

extern "C" esp_err_t __wrap_i2s_channel_write(i2s_chan_handle_t handle,
    const void* src,size_t size,size_t* bytes_written,uint32_t timeout_ms) {
  const bool tracked=isSpeaker(handle);
  size_t accepted=0;
  const esp_err_t result=__real_i2s_channel_write(handle,src,size,&accepted,timeout_ms);
  if (bytes_written) *bytes_written=accepted;
  if (tracked) {
    writeCalls.fetch_add(1,std::memory_order_relaxed);
    requestedBytes.fetch_add(static_cast<uint32_t>(size),std::memory_order_relaxed);
    writtenBytes.fetch_add(static_cast<uint32_t>(accepted),std::memory_order_relaxed);
    lastWriteResult.store(result,std::memory_order_relaxed);
    if (accepted<size) {
      shortWrites.fetch_add(1,std::memory_order_relaxed);
      if (timeout_ms!=0) blockingShortWrites.fetch_add(1,std::memory_order_relaxed);
    }
    if (result!=ESP_OK) {
      writeErrors.fetch_add(1,std::memory_order_relaxed);
      if (timeout_ms!=0) blockingErrors.fetch_add(1,std::memory_order_relaxed);
      lastWriteError.store(result,std::memory_order_relaxed);
      int32_t expected=ESP_OK;
      firstWriteError.compare_exchange_strong(expected,result,std::memory_order_relaxed);
      if (result==ESP_ERR_TIMEOUT) {
        writeTimeouts.fetch_add(1,std::memory_order_relaxed);
        if (timeout_ms==0) nonblockingRetries.fetch_add(1,std::memory_order_relaxed);
      }
    }
    recordSamples(src,accepted<size ? accepted : size);
  } else otherWriteCalls.fetch_add(1,std::memory_order_relaxed);
  return result;
}

namespace AudioProbe {
Snapshot snapshot() {
  Snapshot result;
  const auto handle=speaker.load(std::memory_order_relaxed);
  result.tracked=handle!=nullptr;
  i2s_chan_info_t info{};
  result.infoResult=i2s_channel_get_info(handle,&info);
  result.infoOk=result.infoResult==ESP_OK;
  if (result.infoOk) {
    result.enabled=info.is_enabled;
    result.port=info.id; result.direction=info.dir; result.mode=info.mode;
    result.sourceHz=info.sclk_hz; result.mclkHz=info.mclk_hz; result.bclkHz=info.bclk_hz;
    // This firmware keeps the initialized speaker channel alive. mode_cfg is
    // driver-owned; inspect it only in this diagnostic call, never in wrappers.
    if (info.mode==I2S_COMM_MODE_STD && info.mode_cfg)
      result.sampleRate=static_cast<const i2s_std_config_t*>(info.mode_cfg)->clk_cfg.sample_rate_hz;
  }
#define AUDIO_PROBE_LOAD(name) result.name=name.load(std::memory_order_relaxed)
  AUDIO_PROBE_LOAD(enableCalls); AUDIO_PROBE_LOAD(enableErrors);
  AUDIO_PROBE_LOAD(writeCalls); AUDIO_PROBE_LOAD(writeErrors);
  AUDIO_PROBE_LOAD(writeTimeouts); AUDIO_PROBE_LOAD(shortWrites);
  AUDIO_PROBE_LOAD(nonblockingRetries); AUDIO_PROBE_LOAD(blockingErrors);
  AUDIO_PROBE_LOAD(blockingShortWrites);
  AUDIO_PROBE_LOAD(requestedBytes); AUDIO_PROBE_LOAD(writtenBytes);
  AUDIO_PROBE_LOAD(nonzeroSamples); AUDIO_PROBE_LOAD(peak);
  AUDIO_PROBE_LOAD(lastEnableResult); AUDIO_PROBE_LOAD(lastWriteResult);
  AUDIO_PROBE_LOAD(firstWriteError); AUDIO_PROBE_LOAD(lastWriteError);
  AUDIO_PROBE_LOAD(otherEnableCalls); AUDIO_PROBE_LOAD(otherWriteCalls);
#undef AUDIO_PROBE_LOAD
  return result;
}
}  // namespace AudioProbe
