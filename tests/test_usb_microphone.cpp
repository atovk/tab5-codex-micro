#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <functional>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>
#include "usb_microphone.cpp"

namespace {
bool initialized=true,mounted=true,suspended=false,audioMounted=true;
std::vector<uint8_t> controlData;
std::vector<int16_t> outputPcm;
std::vector<uint8_t> fifoStorage(6400);
tu_fifo_t fakeFifo{};
unsigned clears=0;
unsigned dmaPublishes=0;
uint16_t threshold=0;
std::mutex pauseMutex;
std::condition_variable pauseChanged;
bool pauseNextPublish=false,publishPaused=false,releasePublish=false;
std::function<void()> delayAction;
}
extern "C" bool tud_mounted() {return mounted;}
extern "C" bool tud_inited() {return initialized;}
extern "C" bool tud_suspended() {return suspended;}
extern "C" bool tud_audio_n_mounted(uint8_t) {return audioMounted;}
extern "C" bool tud_audio_n_clear_ep_in_ff(uint8_t) {
  // SDK tu_fifo_clear takes its installed WR mutex, then its optional RD mutex.
  // Keep this behavior so calling clear while holding WR exposes recursion.
  assert(fakeFifo.mutex_wr && !fakeFifo.mutex_rd);
  assert(xSemaphoreTake(fakeFifo.mutex_wr,portMAX_DELAY)==pdTRUE);
  ++clears; fakeFifo.rd_idx=fakeFifo.wr_idx=0; outputPcm.clear();
  xSemaphoreGive(fakeFifo.mutex_wr); return true;
}
extern "C" uint16_t tud_audio_n_write(uint8_t,const void*,uint16_t) {
  assert(false && "publish must not recursively call the SDK FIFO writer"); return 0;
}
extern "C" tu_fifo_t* tud_audio_n_get_ep_in_ff(uint8_t) {return &fakeFifo;}
extern "C" void tud_audio_n_set_ep_in_fifo_threshold(uint8_t,uint16_t value) {threshold=value;}
extern "C" void tu_fifo_get_write_info(tu_fifo_t* fifo,tu_fifo_buffer_info_t* info) {
  assert(fifo==&fakeFifo && info && fifo->mutex_wr);
  assert(fifo->mutex_wr->owner.load()==semaphoreThread());
  {
    std::unique_lock<std::mutex> lock(pauseMutex);
    if (pauseNextPublish) {
      pauseNextPublish=false; publishPaused=true; pauseChanged.notify_all();
      assert(pauseChanged.wait_for(lock,std::chrono::seconds(3),[] {return releasePublish;}));
    }
  }
  const uint16_t free=tu_fifo_remaining(fifo);
  const uint16_t offset=fifo->wr_idx%fifo->depth;
  info->linear.len=std::min<uint16_t>(free,fifo->depth-offset);
  info->linear.ptr=fifo->buffer+offset;
  info->wrapped.len=free-info->linear.len;
  info->wrapped.ptr=fifo->buffer;
}
extern "C" void tu_fifo_advance_write_pointer(tu_fifo_t* fifo,uint16_t count) {
  assert(fifo==&fakeFifo && fifo->mutex_wr && count%2==0);
  assert(fifo->mutex_wr->owner.load()==semaphoreThread());
  assert(count<=tu_fifo_remaining(fifo));
  for (uint16_t at=0;at<count;at+=2) {
    uint8_t bytes[2]={fifo->buffer[(fifo->wr_idx+at)%fifo->depth],
                      fifo->buffer[(fifo->wr_idx+at+1)%fifo->depth]};
    int16_t sample=0; std::memcpy(&sample,bytes,sizeof(sample)); outputPcm.push_back(sample);
  }
  fifo->wr_idx=(fifo->wr_idx+count)%(2*fifo->depth); ++dmaPublishes;
}
extern "C" bool tud_audio_buffer_and_schedule_control_xfer(uint8_t,const tusb_control_request_t*,void* data,uint16_t length) {
  const auto* bytes=static_cast<const uint8_t*>(data);
  controlData.assign(bytes,bytes+length); return true;
}

namespace {
void iterations(int n=1) {
  runIterations=n;
  try {transmit(nullptr);} catch (const EndIteration&) {}
}
tusb_control_request_t request(uint16_t index,uint16_t value,uint8_t operation=0,uint16_t length=0) {
  tusb_control_request_t r{};
  r.wIndex=index; r.wValue=value; r.bRequest=operation; r.wLength=length; return r;
}
void start(bool active=true) {
  const auto r=request(streamInterface,active?1:0);
  assert(tud_audio_set_itf_cb(0,&r));
}
void initialize() {
  fakeFifo.depth=6400;
  fakeFifo.buffer=fifoStorage.data();
  assert(UsbMicrophone::begin());
  uint8_t bytes[256]{}, interface=4;
  assert(descriptorLoader && descriptorLoader(bytes,&interface)==registeredLength);
  assert(interface==6 && controlInterface==4 && streamInterface==5 && endpoint==0x83);
}
void descriptorTest() {
  uint8_t bytes[256]{}, interface=4;
  const size_t length=descriptorLoader(bytes,&interface);
  unsigned standardInterfaces=0,endpoints=0,featureUnits=0,formats=0;
  size_t totalControlLength=0,advertisedControlLength=0;
  bool inControl=false;
  for (size_t at=0;at<length;at+=bytes[at]) {
    const auto* d=bytes+at;
    assert(d[0]>=2 && at+d[0]<=length);
    if (d[1]==TUSB_DESC_INTERFACE) {
      ++standardInterfaces;
      assert(d[5]==TUSB_CLASS_AUDIO && d[7]==0);
      inControl=d[6]==AUDIO_SUBCLASS_CONTROL;
      if (inControl) assert(d[2]==4 && d[3]==0 && d[4]==0);
      else assert(d[2]==5 && d[3]<=1 && d[4]==d[3]);
    } else if (d[1]==TUSB_DESC_CS_INTERFACE && inControl) {
      totalControlLength+=d[0];
      if (d[2]==AUDIO10_CS_AC_INTERFACE_HEADER) {
        assert(d[3]==0 && d[4]==1 && d[7]==1 && d[8]==5);
        advertisedControlLength=uint16_t(d[5]) | (uint16_t(d[6])<<8);
      } else if (d[2]==AUDIO10_CS_AC_INTERFACE_FEATURE_UNIT) {
        ++featureUnits;
        assert(d[3]==2 && d[4]==1 && d[5]==2);
        assert(d[6]==3 && d[7]==0 && d[8]==3 && d[9]==0);
      }
    } else if (d[1]==TUSB_DESC_CS_INTERFACE && d[2]==AUDIO10_CS_AS_INTERFACE_FORMAT_TYPE) {
      ++formats;
      assert(d[4]==1 && d[5]==2 && d[6]==16 && d[7]==1);
      assert((uint32_t(d[8]) | (uint32_t(d[9])<<8) | (uint32_t(d[10])<<16))==16000);
    } else if (d[1]==TUSB_DESC_ENDPOINT) {
      ++endpoints;
      assert(d[2]==0x83 && (d[3]&3)==TUSB_XFER_ISOCHRONOUS);
      assert(d[4]==34 && d[5]==0 && d[6]==1);
    }
  }
  assert(standardInterfaces==3 && endpoints==1 && featureUnits==1 && formats==1);
  assert(totalControlLength==advertisedControlLength);
}
void controlsTest() {
  initialized=false;
  iterations(2); assert(clears==0);
  initialized=true;
  assert(!UsbMicrophone::status().streaming);
  assert(UsbMicrophone::status().mounted);
  start(); assert(UsbMicrophone::status().streaming && threshold==160);
  auto r=request(controlInterface,0); assert(tud_audio_set_itf_cb(0,&r));
  assert(UsbMicrophone::status().streaming);
  r=request(streamInterface,2); assert(!tud_audio_set_itf_cb(0,&r));
  uint8_t value=1;
  r=request((2<<8)|controlInterface,AUDIO10_FU_CTRL_MUTE<<8,AUDIO10_CS_REQ_SET_CUR,1);
  assert(tud_audio_set_req_entity_cb(0,&r,&value));
  auto s=UsbMicrophone::status(); assert(s.hostMuted && !s.muted && s.streaming);
  UsbMicrophone::setMuted(true); value=0;
  assert(tud_audio_set_req_entity_cb(0,&r,&value));
  s=UsbMicrophone::status(); assert(!s.hostMuted && s.muted);
  r.wValue|=2; assert(!tud_audio_set_req_entity_cb(0,&r,&value));
  r=request((2<<8)|controlInterface,AUDIO10_FU_CTRL_VOLUME<<8,AUDIO10_CS_REQ_GET_MIN,2);
  assert(tud_audio_get_req_entity_cb(0,&r));
  assert(controlData.size()==2 && int16_t(controlData[0]|uint16_t(controlData[1])<<8)==-24*256);
  r.wLength=1; assert(!tud_audio_get_req_entity_cb(0,&r));
  r=request(endpoint,AUDIO10_EP_CTRL_SAMPLING_FREQ<<8,AUDIO10_CS_REQ_GET_CUR,3);
  assert(tud_audio_get_req_ep_cb(0,&r));
  assert(controlData==std::vector<uint8_t>({0x80,0x3e,0}));
  r.bRequest=AUDIO10_CS_REQ_SET_CUR;
  uint8_t goodRate[]={0x80,0x3e,0},badRate[]={0x44,0xac,0};
  assert(tud_audio_set_req_ep_cb(0,&r,goodRate));
  assert(!tud_audio_set_req_ep_cb(0,&r,badRate));
  start(false); assert(!UsbMicrophone::status().streaming);
}
void epochsTest() {
  int16_t samples[160]; std::fill(samples,samples+160,1234);
  start(); iterations();
  auto epoch=UsbMicrophone::status().generation;
  assert(UsbMicrophone::write(samples,160,epoch)==160);
  start(false); start();
  assert(UsbMicrophone::write(samples,160,epoch)==0);
  iterations(); assert(outputPcm.empty() && UsbMicrophone::status().queuedBlocks==0);
  epoch=UsbMicrophone::status().generation;
  assert(UsbMicrophone::write(samples,160,epoch)==160);
  eventHandler(nullptr,ARDUINO_USB_EVENTS,ARDUINO_USB_STOPPED_EVENT,nullptr);
  assert(!UsbMicrophone::status().streaming);
  start(); iterations(); assert(outputPcm.empty());
  epoch=UsbMicrophone::status().generation;
  assert(UsbMicrophone::write(samples,160,epoch)==160);
  suspended=true; eventHandler(nullptr,ARDUINO_USB_EVENTS,ARDUINO_USB_SUSPEND_EVENT,nullptr);
  assert(!UsbMicrophone::status().streaming);
  suspended=false; eventHandler(nullptr,ARDUINO_USB_EVENTS,ARDUINO_USB_RESUME_EVENT,nullptr);
  iterations(); assert(outputPcm.empty());
  assert(clears>=4);
}
void pcmTest() {
  int16_t samples[160]; std::fill(samples,samples+160,10000);
  start(); iterations();
  auto r=request((2<<8)|controlInterface,AUDIO10_FU_CTRL_VOLUME<<8,AUDIO10_CS_REQ_SET_CUR,2);
  uint8_t db[]={0,0xfa}; // -6dB in signed 8.8.
  assert(tud_audio_set_req_entity_cb(0,&r,db));
  assert(UsbMicrophone::write(samples,160)==160);
  iterations(); assert(outputPcm.size()==160);
  for (auto value:outputPcm) assert(value>=5011 && value<=5013);
  UsbMicrophone::setMuted(true); assert(UsbMicrophone::write(samples,160)==0);
  iterations(11); assert(!outputPcm.empty());
  for (auto value:outputPcm) assert(value==0);
  assert(UsbMicrophone::status().txPackets==0);
  tud_audio_tx_done_isr(0,32,0,endpoint,1);
  tud_audio_tx_done_isr(0,32,1,endpoint,1);
  tud_audio_tx_done_isr(0,32,0,endpoint,0);
  assert(UsbMicrophone::status().txPackets==1 && UsbMicrophone::status().txBytes==32);
}
void assertInstalledFence() {
  assert(fakeFifo.mutex_wr && fakeFifo.mutex_wr==fifoMutex);
  assert(fakeFifo.mutex_rd==nullptr && fifoReady.load());
}
void publishRaceTest(bool localMute) {
  start(); iterations(); assertInstalledFence();
  int16_t oldPcm[160]; std::fill(oldPcm,oldPcm+160,1234);
  const uint32_t oldEpoch=UsbMicrophone::status().generation;
  assert(UsbMicrophone::write(oldPcm,160,oldEpoch)==160);
  {
    std::lock_guard<std::mutex> lock(pauseMutex);
    pauseNextPublish=true; publishPaused=false; releasePublish=false;
  }
  std::thread tx([] {iterations();});
  {
    std::unique_lock<std::mutex> lock(pauseMutex);
    assert(pauseChanged.wait_for(lock,std::chrono::seconds(3),[] {return publishPaused;}));
  }
  const unsigned previousBlocking=fakeFifo.mutex_wr->blockingTakes.load();
  const unsigned previousClears=clears;
  std::atomic<bool> controlDone{false};
  std::thread control([&] {
    if (localMute) UsbMicrophone::setMuted(true);
    else {
      const auto close=request(streamInterface,0);
      assert(tud_audio_set_itf_close_ep_cb(0,&close));
      start(false); start();
    }
    controlDone.store(true);
  });
  // Wait until the control thread really attempts the shared mutex. No
  // sleep-based guess and no recursive, same-thread callback injection.
  const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
  while (fakeFifo.mutex_wr->blockingTakes.load()==previousBlocking
         && std::chrono::steady_clock::now()<deadline) std::this_thread::yield();
  assert(fakeFifo.mutex_wr->blockingTakes.load()>previousBlocking);
  assert(!controlDone.load());
  assert(UsbMicrophone::status().generation==oldEpoch);
  // ISR accounting must remain usable while the producer/control fence is held.
  const unsigned beforeIsrLocks=fakeFifo.mutex_wr->blockingTakes.load();
  tud_audio_tx_done_isr(0,32,0,endpoint,1);
  assert(fakeFifo.mutex_wr->blockingTakes.load()==beforeIsrLocks);
  {
    std::lock_guard<std::mutex> lock(pauseMutex);
    releasePublish=true; pauseChanged.notify_all();
  }
  tx.join(); control.join();
  assert(controlDone.load() && clears>previousClears);
  assert(tu_fifo_empty(&fakeFifo) && outputPcm.empty());
  assert(UsbMicrophone::status().generation!=oldEpoch);
  assert(UsbMicrophone::write(oldPcm,160,oldEpoch)==0);
  if (localMute) UsbMicrophone::setMuted(false);
  int16_t newPcm[160]; std::fill(newPcm,newPcm+160,2345);
  assert(UsbMicrophone::write(newPcm,160,UsbMicrophone::status().generation)==160);
  iterations();
  assert(outputPcm.size()==160);
  for (const auto sample:outputPcm) assert(sample==2345);
}
void primeSecondIteration(std::function<void()> action) {
  // Keep the real TX worker alive across two iterations. Seeding after its
  // first iteration avoids depending on its initial generation/FIFO clear.
  delayAction=std::move(action);
  onTaskDelay=[] {
    if (delayAction) {
      auto action=std::move(delayAction); delayAction={}; action();
    }
  };
  iterations(2); onTaskDelay=nullptr;
}
void fifoWrapTest() {
  start(); assertInstalledFence();
  int16_t pcm[160];
  for (unsigned i=0;i<160;++i) pcm[i]=int16_t(1000+i);
  const uint16_t offset=fakeFifo.depth-100;
  primeSecondIteration([&] {
    std::fill(fifoStorage.begin(),fifoStorage.end(),0xA5);
    fakeFifo.rd_idx=fakeFifo.wr_idx=offset;
    assert(UsbMicrophone::write(pcm,160)==160);
  });
  assert(outputPcm.size()==160 && tu_fifo_count(&fakeFifo)==320);
  assert(fakeFifo.rd_idx==offset && fakeFifo.wr_idx==offset+320);
  for (unsigned i=0;i<160;++i) assert(outputPcm[i]==pcm[i]);
  const auto* expected=reinterpret_cast<const uint8_t*>(pcm);
  for (unsigned i=0;i<320;++i) assert(fifoStorage[(offset+i)%fakeFifo.depth]==expected[i]);
  assert(fifoStorage[220]==0xA5 && fifoStorage[offset-1]==0xA5);
}
void fifoFullTest() {
  start(); assertInstalledFence();
  int16_t pcm[160]; std::fill(pcm,pcm+160,5432);
  auto dropped=UsbMicrophone::status().droppedSamples;
  std::chrono::steady_clock::time_point began;
  primeSecondIteration([&] {
    // A full, small FIFO reaches the zero-space DMA path without invoking the
    // separate stale-backlog trim policy.
    fakeFifo.depth=320; fakeFifo.rd_idx=0; fakeFifo.wr_idx=320;
    std::fill(fifoStorage.begin(),fifoStorage.end(),0xA5);
    assert(UsbMicrophone::write(pcm,160)==160);
    began=std::chrono::steady_clock::now();
  });
  assert(std::chrono::steady_clock::now()-began<std::chrono::milliseconds(100));
  assert(outputPcm.empty() && fakeFifo.wr_idx==320 && tu_fifo_full(&fakeFifo));
  assert(UsbMicrophone::status().droppedSamples-dropped==160);
  for (unsigned i=0;i<320;++i) assert(fifoStorage[i]==0xA5);

  fakeFifo.depth=uint16_t(fifoStorage.size());
  std::mutex holderMutex;
  std::condition_variable holderChanged;
  bool holding=false,release=false;
  std::thread holder;
  dropped=UsbMicrophone::status().droppedSamples;
  unsigned tries=0,blocking=0;
  primeSecondIteration([&] {
    holder=std::thread([&] {
      assert(xSemaphoreTake(fakeFifo.mutex_wr,portMAX_DELAY)==pdTRUE);
      {
        std::unique_lock<std::mutex> lock(holderMutex);
        holding=true; holderChanged.notify_all();
        assert(holderChanged.wait_for(lock,std::chrono::seconds(3),[&] {return release;}));
      }
      xSemaphoreGive(fakeFifo.mutex_wr);
    });
    {
      std::unique_lock<std::mutex> lock(holderMutex);
      assert(holderChanged.wait_for(lock,std::chrono::seconds(3),[&] {return holding;}));
    }
    tries=fakeFifo.mutex_wr->tryTakes.load();
    blocking=fakeFifo.mutex_wr->blockingTakes.load();
    assert(UsbMicrophone::write(pcm,160)==160);
    began=std::chrono::steady_clock::now();
  });
  const auto elapsed=std::chrono::steady_clock::now()-began;
  assert(fakeFifo.mutex_wr->tryTakes.load()==tries+1);
  assert(fakeFifo.mutex_wr->blockingTakes.load()==blocking);
  assert(UsbMicrophone::status().droppedSamples-dropped==160 && outputPcm.empty());
  {
    std::lock_guard<std::mutex> lock(holderMutex); release=true; holderChanged.notify_all();
  }
  holder.join();
  assert(elapsed<std::chrono::milliseconds(100));
}
void queueTest() {
  PcmQueue q; PcmQueue::Block block;
  int16_t input[160]{};
  assert(!q.push(nullptr,1,1) && !q.push(input,0,1) && !q.push(input,161,1));
  for (unsigned cycle=0;cycle<1000;++cycle) {
    for (unsigned i=0;i<q.capacity;++i) {
      input[0]=int16_t(i+cycle); assert(q.push(input,160,cycle));
    }
    assert(q.size()==q.capacity && !q.push(input,160,0));
    for (unsigned i=0;i<q.capacity;++i) {
      assert(q.pop(block) && block.generation==cycle && block.count==160);
      assert(block.samples[0]==int16_t(i+cycle));
    }
    assert(q.size()==0 && !q.pop(block));
  }
  constexpr uint32_t count=50000;
  std::thread producer([&] {
    for (uint32_t n=1;n<=count;++n) {
      for (size_t i=0;i<160;++i) input[i]=int16_t(n+i);
      while (!q.push(input,160,n)) std::this_thread::yield();
    }
  });
  for (uint32_t n=1;n<=count;++n) {
    while (!q.pop(block)) std::this_thread::yield();
    assert(block.count==160 && block.generation==n);
    for (size_t i=0;i<160;++i) assert(block.samples[i]==int16_t(n+i));
  }
  producer.join(); assert(q.size()==0);
}
} // namespace
int main(int argc,char** argv) {
  assert(argc==2); initialize();
  const std::string test=argv[1];
  if (test=="descriptor") descriptorTest();
  else if (test=="controls") controlsTest();
  else if (test=="epochs") epochsTest();
  else if (test=="pcm") pcmTest();
  else if (test=="queue") queueTest();
  else if (test=="publish-reopen") publishRaceTest(false);
  else if (test=="publish-mute") publishRaceTest(true);
  else if (test=="fifo-wrap") fifoWrapTest();
  else if (test=="fifo-full") fifoFullTest();
  else return 2;
  std::cout<<"USB microphone contract passed\n";
}
