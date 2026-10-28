#include "usb_microphone.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <USB.h>
#include <esp32-hal-tinyusb.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <tusb.h>

namespace {
using UsbMicrophone::Detail::PcmQueue;
constexpr uint16_t descriptorLength=TUD_AUDIO10_MIC_ONE_CH_DESC_LEN(1);
constexpr uint16_t packetBytes=(UsbMicrophone::sampleRate/1000+1)*sizeof(int16_t);
constexpr uint16_t fifoTarget=UsbMicrophone::blockSamples;
constexpr uint16_t maxQueuedBytes=UsbMicrophone::blockSamples*sizeof(int16_t)*2;
constexpr int16_t minVolume=-24*256,maxVolume=0,volumeStep=256;
constexpr uint8_t featureUnit=2;
std::atomic<bool> available{false},altActive{false},localMuted{false};
std::atomic<bool> hostMute[2]{};
std::atomic<int16_t> hostVolume[2]{};
std::atomic<uint32_t> generation{1},writtenSamples{0},droppedSamples{0};
std::atomic<uint32_t> txPackets{0},txBytes{0};
std::atomic<bool> fifoReady{false};
SemaphoreHandle_t fifoMutex=nullptr;
uint8_t controlInterface=0,streamInterface=0,endpoint=0;
PcmQueue queue;

bool hostMuted() { return hostMute[0].load() || hostMute[1].load(); }
bool streaming() {
  return available.load() && altActive.load() && tud_mounted() && !tud_suspended()
      && tud_audio_mounted();
}
class FifoGuard {
 public:
  explicit FifoGuard(TickType_t wait=portMAX_DELAY)
      : locked(fifoMutex && xSemaphoreTake(fifoMutex,wait)==pdTRUE) {}
  ~FifoGuard() { if (locked) xSemaphoreGive(fifoMutex); }
  const bool locked;
};
void clearFifo() {
  if (fifoReady.load(std::memory_order_acquire) && tud_inited()) tud_audio_clear_ep_in_ff();
}
void invalidateLocked() { generation.fetch_add(1,std::memory_order_acq_rel); }
void invalidate() {
  { FifoGuard guard; invalidateLocked(); }
  clearFifo();
}
void resetHost() {
  {
    FifoGuard guard;
    altActive.store(false);
    for (int i=0;i<2;++i) { hostMute[i].store(false); hostVolume[i].store(0); }
    invalidateLocked();
  }
  clearFifo();
}
bool configureFifo() {
  if (fifoReady.load(std::memory_order_acquire)) return true;
  auto* fifo=tud_audio_n_get_ep_in_ff(0);
  if (!fifo || !fifoMutex) return false;
#if OSAL_MUTEX_REQUIRED
  // Called only by the TinyUSB task at the first SET_INTERFACE. The SDK has
  // initialized this FIFO, but no application producer has used it yet; its
  // first transfer is a ZLP. Keep the ISR consumer's read side lock-free.
  if (fifo->mutex_wr || fifo->mutex_rd) return false;
  tu_fifo_config_mutex(fifo,fifoMutex,nullptr);
#endif
  fifoReady.store(true,std::memory_order_release);
  return true;
}
uint16_t publish(tu_fifo_t* fifo,const int16_t* pcm,uint16_t bytes) {
  // Caller owns the SDK FIFO's WR mutex. Its own clear/reset operations take
  // this same mutex. The USB ISR remains a normal lock-free single consumer.
  // Do not call tud_audio_write here: it would recursively take that mutex.
  tu_fifo_buffer_info_t info{};
  tu_fifo_get_write_info(fifo,&info);
  const uint16_t count=std::min<uint16_t>(bytes,uint16_t(info.linear.len+info.wrapped.len))&~uint16_t(1);
  const uint16_t linear=std::min(count,info.linear.len);
  if (linear) std::memcpy(info.linear.ptr,pcm,linear);
  if (count>linear) std::memcpy(info.wrapped.ptr,reinterpret_cast<const uint8_t*>(pcm)+linear,count-linear);
  std::atomic_thread_fence(std::memory_order_release);
  tu_fifo_advance_write_pointer(fifo,count);
  return count;
}
uint16_t loadDescriptor(uint8_t* output,uint8_t* interface) {
  controlInterface=*interface; streamInterface=uint8_t(*interface+1);
  const uint8_t ep=tinyusb_get_free_in_endpoint();
  if (!ep || ep>6) return 0; // Tab5's FS controller, regardless of P4 HS SDK defaults.
  endpoint=uint8_t(ep|0x80);
  const uint8_t string=tinyusb_add_string_descriptor("Tab5 Microphone");
  // The SDK macro promotes its dynamic interface-number + 1 to int. Expand
  // into ints first, then narrow byte values explicitly (also valid in C++17).
  const int descriptor[]={
    TUD_AUDIO10_MIC_ONE_CH_DESCRIPTOR(controlInterface,string,2,16,endpoint,packetBytes,
                                     UsbMicrophone::sampleRate)
  };
  static_assert(sizeof(descriptor)/sizeof(descriptor[0])==descriptorLength,"UAC1 descriptor length changed");
  for (size_t i=0;i<descriptorLength;++i) output[i]=uint8_t(descriptor[i]);
  *interface+=2;
  return descriptorLength;
}
void usbEvent(void*,esp_event_base_t base,int32_t id,void*) {
  if (base!=ARDUINO_USB_EVENTS) return;
  if (id==ARDUINO_USB_STOPPED_EVENT) resetHost();
  else if (id==ARDUINO_USB_SUSPEND_EVENT || id==ARDUINO_USB_RESUME_EVENT) invalidate();
}

void transmit(void*) {
  uint32_t observed=0;
  TickType_t lastSilence=0;
  PcmQueue::Block block;
  int16_t pcm[UsbMicrophone::blockSamples]{};
  for (;;) {
    // begin() precedes USB.begin(). Only SET_INTERFACE installs the FIFO fence.
    if (!tud_inited() || !fifoReady.load(std::memory_order_acquire)) { vTaskDelay(1); continue; }
    const uint32_t current=generation.load(std::memory_order_acquire);
    if (observed!=current) {
      clearFifo(); observed=current; lastSilence=0;
    }
    if (!streaming()) {
      while (queue.pop(block)) droppedSamples.fetch_add(block.count);
      vTaskDelay(1); continue;
    }
    const bool silence=localMuted.load() || hostMuted();
    size_t count=0;
    if (silence) {
      while (queue.pop(block)) droppedSamples.fetch_add(block.count);
      const TickType_t now=xTaskGetTickCount();
      if (now-lastSilence>=pdMS_TO_TICKS(10)) {
        std::fill(pcm,pcm+UsbMicrophone::blockSamples,0);
        count=UsbMicrophone::blockSamples; lastSilence=now;
      }
    } else if (queue.pop(block)) {
      if (block.generation==current) {
        const int volume=hostVolume[0].load()+hostVolume[1].load();
        const int32_t gain=int32_t(std::lround(std::pow(10.f,float(volume)/(256.f*20.f))*32768.f));
        count=block.count;
        for (size_t i=0;i<count;++i) pcm[i]=int16_t((int32_t(block.samples[i])*gain)/32768);
      } else droppedSamples.fetch_add(block.count);
    }
    if (count) {
      bool trim=false;
      {
        // A control transition owns this same SDK producer mutex while changing
        // generation, then clears the FIFO before returning. The ADC producer
        // never takes this lock; the TX worker skips immediately if it is busy.
        FifoGuard guard(0);
        if (guard.locked && current==generation.load(std::memory_order_acquire) && streaming()) {
          if (auto* fifo=tud_audio_n_get_ep_in_ff(0)) {
            if (tu_fifo_count(fifo)>maxQueuedBytes) {
              droppedSamples.fetch_add(tu_fifo_count(fifo)/sizeof(int16_t)); trim=true;
            } else {
              const uint16_t bytes=publish(fifo,pcm,uint16_t(count*sizeof(int16_t)));
              writtenSamples.fetch_add(bytes/sizeof(int16_t));
              droppedSamples.fetch_add(count-bytes/sizeof(int16_t)); count=0;
            }
          }
        }
      }
      if (count) droppedSamples.fetch_add(count);
      if (trim) clearFifo(); // The SDK takes WR mutex; never call while holding it.
    }
    vTaskDelay(1);
  }
}
bool featureRequest(const tusb_control_request_t* request) {
  return uint8_t(request->wIndex)==controlInterface && uint8_t(request->wIndex>>8)==featureUnit
      && uint8_t(request->wValue)<=1;
}
bool endpointRequest(const tusb_control_request_t* request) {
  return request->wIndex==endpoint && uint8_t(request->wValue>>8)==AUDIO10_EP_CTRL_SAMPLING_FREQ;
}
} // namespace

namespace UsbMicrophone {
bool begin() {
  if (available.load()) return true;
  if (!fifoMutex) fifoMutex=xSemaphoreCreateMutex();
  if (!fifoMutex) return false;
  if (tinyusb_enable_interface(USB_INTERFACE_AUDIO,descriptorLength,loadDescriptor)!=ESP_OK) return false;
  USB.onEvent(usbEvent);
  TaskHandle_t task=nullptr;
  if (xTaskCreate(transmit,"usb-mic-tx",4096,nullptr,2,&task)!=pdPASS) return false;
  available.store(true); return true;
}
size_t write(const int16_t* samples,size_t count) {
  return write(samples,count,generation.load(std::memory_order_acquire));
}
size_t write(const int16_t* samples,size_t count,uint32_t expectedGeneration) {
  if (!streaming() || localMuted.load() || hostMuted()) return 0;
  const uint32_t current=generation.load(std::memory_order_acquire);
  if (current!=expectedGeneration) { droppedSamples.fetch_add(count); return 0; }
  if (!queue.push(samples,count,current)) { droppedSamples.fetch_add(count); return 0; }
  return count;
}
void setMuted(bool value) {
  if (localMuted.load()==value) return;
  bool changed=false;
  {
    FifoGuard guard;
    changed=localMuted.exchange(value)!=value;
    if (changed) invalidateLocked();
  }
  if (changed) clearFifo();
}
Status status() {
  Status result;
  result.available=available.load(); result.mounted=tud_mounted(); result.streaming=streaming();
  result.muted=localMuted.load(); result.hostMuted=hostMuted();
  result.hostVolumeDb=(hostVolume[0].load()+hostVolume[1].load())/256;
  result.generation=generation.load(); result.queuedBlocks=queue.size();
  result.writtenSamples=writtenSamples.load(); result.droppedSamples=droppedSamples.load();
  result.txPackets=txPackets.load(); result.txBytes=txBytes.load();
  return result;
}
} // namespace UsbMicrophone

extern "C" bool tud_audio_set_itf_close_ep_cb(uint8_t,const tusb_control_request_t* request) {
  if (request->wIndex!=streamInterface) return true;
  { FifoGuard guard; altActive.store(false); invalidateLocked(); }
  clearFifo(); return true;
}
extern "C" bool tud_audio_set_itf_cb(uint8_t,const tusb_control_request_t* request) {
  if (request->wIndex==controlInterface) return request->wValue==0;
  if (request->wIndex!=streamInterface || request->wValue>1) return false;
  if (!configureFifo()) return false;
  { FifoGuard guard; altActive.store(request->wValue==1); invalidateLocked(); }
  clearFifo();
  // The SDK default targets half of its HS-sized FIFO (100ms at this rate).
  // Target a single 5ms half-block instead of building that much latency.
  tud_audio_n_set_ep_in_fifo_threshold(0,fifoTarget);
  return true;
}
extern "C" bool tud_audio_get_req_ep_cb(uint8_t rhport,const tusb_control_request_t* request) {
  if (!endpointRequest(request) || request->bRequest!=AUDIO10_CS_REQ_GET_CUR || request->wLength!=3) return false;
  uint8_t rate[]={uint8_t(UsbMicrophone::sampleRate),uint8_t(UsbMicrophone::sampleRate>>8),uint8_t(UsbMicrophone::sampleRate>>16)};
  return tud_audio_buffer_and_schedule_control_xfer(rhport,request,rate,sizeof(rate));
}
extern "C" bool tud_audio_set_req_ep_cb(uint8_t,const tusb_control_request_t* request,uint8_t* data) {
  if (!endpointRequest(request) || request->bRequest!=AUDIO10_CS_REQ_SET_CUR || request->wLength!=3) return false;
  const uint32_t rate=uint32_t(data[0]) | (uint32_t(data[1])<<8) | (uint32_t(data[2])<<16);
  return rate==UsbMicrophone::sampleRate;
}
extern "C" bool tud_audio_get_req_entity_cb(uint8_t rhport,const tusb_control_request_t* request) {
  if (!featureRequest(request)) return false;
  const uint8_t channel=uint8_t(request->wValue),control=uint8_t(request->wValue>>8);
  if (control==AUDIO10_FU_CTRL_MUTE && request->bRequest==AUDIO10_CS_REQ_GET_CUR && request->wLength==1) {
    uint8_t value=hostMute[channel].load();
    return tud_audio_buffer_and_schedule_control_xfer(rhport,request,&value,sizeof(value));
  }
  if (control!=AUDIO10_FU_CTRL_VOLUME || request->wLength!=2) return false;
  int16_t value=0;
  switch (request->bRequest) {
    case AUDIO10_CS_REQ_GET_CUR:value=hostVolume[channel].load();break;
    case AUDIO10_CS_REQ_GET_MIN:value=minVolume;break;
    case AUDIO10_CS_REQ_GET_MAX:value=maxVolume;break;
    case AUDIO10_CS_REQ_GET_RES:value=volumeStep;break;
    default:return false;
  }
  uint8_t bytes[]={uint8_t(value),uint8_t(uint16_t(value)>>8)};
  return tud_audio_buffer_and_schedule_control_xfer(rhport,request,bytes,sizeof(bytes));
}
extern "C" bool tud_audio_set_req_entity_cb(uint8_t,const tusb_control_request_t* request,uint8_t* data) {
  if (!featureRequest(request) || request->bRequest!=AUDIO10_CS_REQ_SET_CUR) return false;
  const uint8_t channel=uint8_t(request->wValue),control=uint8_t(request->wValue>>8);
  if (control==AUDIO10_FU_CTRL_MUTE && request->wLength==1 && data[0]<=1) {
    { FifoGuard guard; hostMute[channel].store(data[0]!=0); invalidateLocked(); }
    clearFifo(); return true;
  }
  if (control==AUDIO10_FU_CTRL_VOLUME && request->wLength==2) {
    const int16_t value=int16_t(uint16_t(data[0]) | (uint16_t(data[1])<<8));
    if (value<minVolume || value>maxVolume) return false;
    hostVolume[channel].store(value); return true;
  }
  return false;
}
extern "C" bool tud_audio_tx_done_isr(uint8_t,uint16_t bytes,uint8_t function,uint8_t ep,uint8_t alt) {
  if (function==0 && ep==endpoint && alt==1) { txPackets.fetch_add(1); txBytes.fetch_add(bytes); }
  return true;
}
