#include "deferred_frame_capture.h"
#include <cassert>
#include <cstdlib>
#include <iostream>
#include <unordered_set>
#include <vector>
using namespace DeferredFrameCapture;
namespace {
std::unordered_set<void*> live;
unsigned calls=0, failAt=0, freed=0;
uint32_t fakeUs=0, fakeMs=0, allocationUs=0;
unsigned serviceCalls=0, mutateAt=0;
uint8_t mutation=0;
uint32_t jumpMs=0;
Context serviceContext;
Evidence* mutableEvidence=nullptr;
const Capture* inspectReleasedOwner=nullptr;
void* allocate(size_t bytes) {
  assert(bytes==Capture::bytes);
  fakeUs+=allocationUs;
  if (++calls==failAt) return nullptr;
  void* pointer=std::malloc(bytes); assert(pointer); assert(live.insert(pointer).second);
  return pointer;
}
void release(void* pointer) {
  if (inspectReleasedOwner) {
    assert(!inspectReleasedOwner->active());
    for (size_t i=0;i<Capture::count;++i) assert(inspectReleasedOwner->frame(i).pixels!=pointer);
  }
  assert(pointer && live.erase(pointer)==1); ++freed; std::free(pointer);
}
void resetAllocator(unsigned failure=0) {
  assert(live.empty()); calls=freed=0; failAt=failure;
  allocationUs=0;
}
Context usb() {
  Context c; c.session=7; c.generation=4;
  c.connected=c.authenticated=c.bonded=true; return c;
}
Context bluetooth() {
  auto c=usb(); c.activeBluetooth=c.renderedBluetooth=c.coherent=true; return c;
}
Context service() {
  ++serviceCalls; fakeUs+=100;
  if (serviceCalls==mutateAt) {
    if (mutation==1) ++serviceContext.generation;
    if (mutation==2) serviceContext.authenticated=false;
    if (mutation==3) serviceContext.blocked=true;
    if (mutation==4) ++serviceContext.session;
    if (mutation==5) serviceContext.activeBluetooth=false;
    if (jumpMs) fakeMs=jumpMs;
    if (mutableEvidence) mutableEvidence->ambient.color=0x123456;
  }
  return serviceContext;
}
uint32_t clockUs() { fakeUs+=10; return fakeUs; }
uint32_t clockMs() { return fakeMs; }
const Hooks hooks{service,clockUs,clockMs};
void resetHooks(Context context=bluetooth()) {
  fakeUs=fakeMs=jumpMs=0; serviceCalls=mutateAt=mutation=0;
  serviceContext=context; mutableEvidence=nullptr;
}
Evidence native() {
  Evidence e; e.session=7; e.generation=4; e.bluetooth=e.nativeSessionSeen=true;
  e.hostLightsKnown=true; e.slots[0]={0x304FFE,128,2,80,0};
  e.ambient={0xFFFFFF,255,2,10,0}; e.neonFrames=42; return e;
}
const uint32_t delays[4]={1000,2000,3000,4000};
bool arm(Capture& capture,uint32_t now=0) {
  return capture.arm(7,delays,now,usb(),allocate,release);
}
std::vector<uint8_t> source(Capture::bytes,0xA5);
void complete(Capture& capture) {
  for (uint32_t delay:delays) assert(capture.capture(delay,bluetooth(),source.data(),native()));
  assert(capture.state()==State::Complete && capture.captured()==4 && capture.stored()==4);
}
}
int main() {
  unsigned tests=0;
  { uint32_t values[5];
    assert(parseUnsignedValues("7 6000 16500 18500 24500",values,5));
    assert(values[0]==7 && values[4]==24500);
    assert(parseUnsignedValues(" \t0 4294967295\t",values,2) && values[1]==UINT32_MAX);
    for (const char* invalid:{"", "-1", "+1", "1.0", "4294967296", "-4294966296",
                              "1x", "1 2", "1\n", "999999999999999999999999999999"})
      assert(!parseUnsignedValues(invalid,values,1));
    assert(!parseUnsignedValues("1",values,2)); ++tests; }
  { resetAllocator(); Capture s; assert(arm(s));
    assert(calls==4 && live.size()==4 && s.armedAt()==0 && s.session()==7);
    assert(!s.capture(999,bluetooth(),source.data(),native()) && s.captured()==0);
    complete(s);
    assert(std::memcmp(s.frame(0).pixels,source.data(),Capture::bytes)==0);
    assert(s.frame(0).evidence.capturedAt==1000 && s.frame(3).evidence.elapsed==4000);
    assert(s.frame(0).evidence.ambient.color==0xFFFFFF && s.frame(0).evidence.neonFrames==42);
    source[0]=0x11; assert(s.frame(0).pixels[0]==0xA5); source[0]=0xA5;
    assert(!s.capture(5000,bluetooth(),source.data(),native())); ++tests; }
  for (unsigned failure=1;failure<=4;++failure) {
    resetAllocator(failure); Capture s; assert(!arm(s));
    assert(s.state()==State::Aborted && s.reason()==Reason::AllocationFailed);
    assert(calls==failure && freed==failure-1 && live.empty() && !s.stored()); ++tests;
  }
  for (unsigned invalid=0;invalid<7;++invalid) {
    resetAllocator(); Capture s; auto c=usb();
    if (invalid==0) c.session=8;
    if (invalid==1) c.connected=false;
    if (invalid==2) c.authenticated=false;
    if (invalid==3) c.bonded=false;
    if (invalid==4) c.blocked=true;
    if (invalid==5) c.switching=true;
    assert(!s.arm(invalid==6 ? 0 : 7,delays,0,c,allocate,release));
    assert(calls==0 && live.empty() && s.reason()==Reason::InvalidRequest); ++tests;
  }
  for (unsigned invalid=0;invalid<5;++invalid) {
    resetAllocator(); Capture s; uint32_t bad[4]={1000,2000,3000,4000};
    if (invalid==0) bad[0]=999;
    if (invalid==1) bad[3]=30001;
    if (invalid==2) bad[1]=bad[0];
    if (invalid==3) bad[2]=1999;
    if (invalid==4) bad[0]=0xFFFFFFFFu;
    assert(!s.arm(7,bad,0,usb(),allocate,release) && calls==0); ++tests;
  }
  { resetAllocator(); Capture s; const uint32_t limits[4]={1000,2000,3000,30000};
    assert(s.arm(7,limits,0,usb(),allocate,release)); s.clear();
    assert(!s.arm(7,delays,0,usb(),nullptr,release));
    assert(!s.arm(7,delays,0,usb(),allocate,nullptr)); assert(live.empty()); ++tests; }
  for (unsigned loss=0;loss<4;++loss) {
    resetAllocator(); Capture s; assert(arm(s)); auto c=bluetooth();
    if (loss==0) ++c.session;
    if (loss==1) c.connected=false;
    if (loss==2) c.authenticated=false;
    if (loss==3) c.bonded=false;
    s.poll(500,c); assert(s.state()==State::Aborted && live.empty());
    assert(s.reason()==(loss==0 ? Reason::SessionChanged : Reason::Disconnected));
    assert(!s.capture(1000,bluetooth(),source.data(),native())); ++tests;
  }
  for (unsigned unsafe=0;unsafe<6;++unsafe) {
    resetAllocator(); Capture s; assert(arm(s)); auto c=bluetooth();
    if (unsafe==0) c.activeBluetooth=false;
    if (unsafe==1) c.switching=true;
    if (unsafe==2) c.blocked=true;
    if (unsafe==3) c.renderedBluetooth=false;
    if (unsafe==4) c.coherent=false;
    assert(!s.capture(1000,c,unsafe==5 ? nullptr : source.data(),native()));
    assert(s.state()==State::Aborted && live.empty() && !s.stored()); ++tests;
  }
  for (unsigned invalid=0;invalid<5;++invalid) {
    resetAllocator(); Capture s; assert(arm(s)); auto e=native();
    if (invalid==0) e.bluetooth=false;
    if (invalid==1) e.preview=true;
    if (invalid==2) e.frozen=true;
    if (invalid==3) ++e.session;
    if (invalid==4) ++e.generation;
    assert(!s.capture(1000,bluetooth(),source.data(),e));
    assert(s.reason()==Reason::UnsafeFrame && live.empty()); ++tests;
  }
  { resetAllocator(); Capture s; assert(arm(s)); auto e=native(); e.nativeSessionSeen=false;
    assert(s.capture(1000,bluetooth(),source.data(),e));
    assert(!s.frame(0).evidence.nativeSessionSeen); // Preserve a missing native stream as evidence.
    auto c=bluetooth(); ++c.generation;
    assert(!s.capture(2000,c,source.data(),native()));
    assert(s.reason()==Reason::TransportChanged && s.stored()==1 && live.size()==1);
    assert(s.frame(0).captured && !s.frame(1).captured); ++tests; }
  { resetAllocator(); Capture s; assert(arm(s));
    assert(s.capture(1000,bluetooth(),source.data(),native()));
    assert(!s.take(0)); // Do not start a binary stream while later samples remain armed.
    s.poll(44999,bluetooth()); assert(s.active());
    s.poll(45000,bluetooth());
    assert(s.reason()==Reason::Deadline && s.stored()==1 && live.size()==1);
    auto pixels=s.take(0); assert(pixels && s.frame(0).read && !s.take(0));
    s.clear(); assert(live.size()==1); release(pixels); assert(live.empty()); ++tests; }
  { resetAllocator(); Capture s; assert(arm(s));
    assert(!s.capture(2500,bluetooth(),source.data(),native()));
    assert(s.reason()==Reason::Deadline && live.empty()); // No catch-up burst.
    ++tests; }
  { resetAllocator(); Capture s; assert(arm(s)); complete(s);
    auto transferred=s.take(1); assert(transferred && !s.take(1) && !s.take(4) && !s.take(999));
    assert(s.stored()==3 && live.size()==4);
    s.poll(119999,usb()); assert(s.stored()==3); // Completion does not require BLE to remain connected.
    s.poll(120000,usb()); assert(s.state()==State::Expired && s.reason()==Reason::Ttl);
    assert(s.stored()==0 && live.size()==1 && s.frame(1).read);
    s.clear(); assert(live.size()==1); release(transferred); ++tests; }
  { resetAllocator(); Capture s; assert(arm(s));
    assert(!arm(s) && calls==4 && s.active()); complete(s);
    assert(!arm(s) && calls==4 && s.state()==State::Complete);
    s.clear(); assert(live.empty() && s.state()==State::Idle && s.captured()==0);
    assert(arm(s)); s.clear(); assert(live.empty()); ++tests; }
  { resetAllocator(); Capture s; const uint32_t start=0xFFFFFF00u; assert(arm(s,start));
    assert(!s.capture(uint32_t(start+999),bluetooth(),source.data(),native()));
    for (uint32_t delay:delays)
      assert(s.capture(uint32_t(start+delay),bluetooth(),source.data(),native()));
    assert(s.frame(3).evidence.elapsed==4000);
    s.poll(uint32_t(start+120000),usb()); assert(s.state()==State::Expired && live.empty()); ++tests; }
  { resetAllocator(); resetHooks(usb()); allocationUs=500; fakeMs=777; Capture s;
    assert(s.arm(7,delays,123,usb(),allocate,release,hooks));
    assert(serviceCalls==5 && s.armedAt()==777 && s.armUs()>2000 && s.armMaxStepUs()>=500);
    assert(s.armMaxStepUs()<s.armUs()); ++tests; }
  for (unsigned checkpoint=1;checkpoint<=5;++checkpoint) {
    resetAllocator(); resetHooks(usb()); mutateAt=checkpoint; mutation=4; Capture s;
    assert(!s.arm(7,delays,0,usb(),allocate,release,hooks));
    assert(s.state()==State::Aborted && s.reason()==Reason::SessionChanged);
    assert(calls==checkpoint-1 && live.empty() && !s.stored()); ++tests;
  }
  { resetAllocator(); resetHooks(usb()); mutateAt=3; mutation=1; Capture s;
    assert(!s.arm(7,delays,0,usb(),allocate,release,hooks));
    assert(s.reason()==Reason::TransportChanged && calls==2 && live.empty()); ++tests; }
  { resetAllocator(); resetHooks(usb()); mutateAt=2; mutation=3; Capture s;
    assert(!s.arm(7,delays,0,usb(),allocate,release,hooks));
    assert(s.reason()==Reason::UnsafeFrame && calls==1 && live.empty()); ++tests; }
  for (unsigned failure=1;failure<=4;++failure) {
    resetAllocator(failure); resetHooks(usb()); Capture s;
    assert(!s.arm(7,delays,0,usb(),allocate,release,hooks));
    assert(s.reason()==Reason::AllocationFailed && serviceCalls==failure+1 && live.empty()); ++tests;
  }
  { resetAllocator(); resetHooks(); fakeMs=1000; Capture s; assert(arm(s));
    auto evidence=native(); mutableEvidence=&evidence; mutateAt=1;
    assert(s.capture(1000,bluetooth(),source.data(),evidence,hooks));
    const unsigned chunks=(Capture::bytes+Capture::chunkBytes-1)/Capture::chunkBytes;
    assert(s.frame(0).chunks==chunks && serviceCalls==chunks+1);
    assert(s.frame(0).copyUs>s.frame(0).maxChunkUs && s.frame(0).maxChunkUs>0);
    assert(evidence.ambient.color==0x123456 && s.frame(0).evidence.ambient.color==0xFFFFFF);
    assert(std::memcmp(s.frame(0).pixels,source.data(),Capture::bytes)==0); ++tests; }
  for (uint8_t change=1;change<=5;++change) {
    resetAllocator(); resetHooks(); fakeMs=1000; Capture s; assert(arm(s));
    mutateAt=2; mutation=change;
    assert(!s.capture(1000,bluetooth(),source.data(),native(),hooks));
    assert(s.state()==State::Aborted && !s.frame(0).captured && s.frame(0).chunks==1);
    assert(live.empty() && !s.stored()); ++tests;
  }
  { resetAllocator(); resetHooks(); fakeMs=1000; Capture s; assert(arm(s));
    mutateAt=1; mutation=3;
    assert(!s.capture(1000,bluetooth(),source.data(),native(),hooks));
    assert(s.reason()==Reason::UnsafeFrame && !s.frame(0).chunks && live.empty()); ++tests; }
  { resetAllocator(); resetHooks(); fakeMs=1000; Capture s; assert(arm(s));
    mutateAt=(Capture::bytes+Capture::chunkBytes-1)/Capture::chunkBytes+1; mutation=2;
    assert(!s.capture(1000,bluetooth(),source.data(),native(),hooks));
    assert(s.reason()==Reason::Disconnected && s.frame(0).chunks==113 && !s.frame(0).captured);
    assert(live.empty()); ++tests; }
  { resetAllocator(); resetHooks(); fakeMs=2000; Capture s; assert(arm(s));
    assert(s.capture(1000,bluetooth(),source.data(),native()));
    mutateAt=2; mutation=1;
    assert(!s.capture(2000,bluetooth(),source.data(),native(),hooks));
    assert(s.reason()==Reason::TransportChanged && s.stored()==1 && live.size()==1);
    assert(s.frame(0).captured && !s.frame(1).captured); ++tests; }
  for (uint32_t expiry:{45000u,120000u}) {
    resetAllocator(); resetHooks(); fakeMs=1000; Capture s; assert(arm(s));
    mutateAt=(Capture::bytes+Capture::chunkBytes-1)/Capture::chunkBytes+1; jumpMs=expiry;
    assert(!s.capture(1000,bluetooth(),source.data(),native(),hooks));
    assert(s.reason()==(expiry==45000 ? Reason::Deadline : Reason::Ttl));
    assert(!s.frame(0).captured && s.frame(0).chunks==113 && live.empty()); ++tests;
  }
  { resetAllocator(); Capture s; assert(arm(s)); inspectReleasedOwner=&s;
    s.clear(); assert(live.empty() && s.state()==State::Idle);
    assert(arm(s)); auto invalid=bluetooth(); ++invalid.session; s.poll(1,invalid);
    assert(live.empty() && s.state()==State::Aborted);
    inspectReleasedOwner=nullptr; ++tests; }
  assert(live.empty());
  std::cout<<tests<<" deferred framebuffer cases passed\n";
}
