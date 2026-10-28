#include "micro_connection.h"
#include "micro_transport.h"
#include <assert.h>
#include <iostream>
#include <string>

using namespace MicroTransport;
int main() {
  unsigned tests=0;
  { Selection s; s.update(true,true,false,0);
    assert(s.active()==Link::Usb && !s.switching() && !s.forceBleDisconnect()); ++tests; }
  { Selection s; s.update(false,true,false,0); assert(s.active()==Link::Bluetooth);
    s.update(true,true,false,1); assert(s.active()==Link::Usb && !s.forceBleDisconnect());
    s.update(false,true,false,2); assert(s.active()==Link::Bluetooth); ++tests; }
  { Selection s; s.update(false,true,false,0); s.update(true,true,true,10);
    assert(s.active()==Link::Bluetooth && s.switching() && s.beginCleanup());
    s.update(true,true,true,20); assert(s.switching() && !s.beginCleanup());
    s.update(true,true,false,21);
    assert(s.active()==Link::Usb && s.changed() && !s.forceBleDisconnect()); ++tests; }
  { Selection s; s.update(false,true,false,0); s.update(true,true,true,100);
    s.update(true,true,true,279); assert(s.switching() && !s.forceBleDisconnect());
    s.update(true,true,true,280); assert(s.active()==Link::Usb && s.forceBleDisconnect());
    s.update(true,true,false,281); assert(!s.forceBleDisconnect()); ++tests; }
  { Selection s; s.update(false,true,false,0); s.update(true,true,true,1);
    s.update(false,true,true,5);
    assert(s.active()==Link::Bluetooth && !s.switching() && !s.forceBleDisconnect()); ++tests; }
  { Selection s; s.update(false,true,false,0); s.update(true,true,true,0xfffffff0u);
    s.update(true,true,true,20); assert(s.switching());
    s.update(true,true,true,164); assert(s.active()==Link::Usb && s.forceBleDisconnect()); ++tests; }
  { Selection s; s.update(false,true,false,0); s.update(true,false,true,1);
    assert(s.active()==Link::Usb && !s.switching() && !s.forceBleDisconnect());
    s.update(false,false,true,2); assert(s.active()==Link::None); ++tests; }
  { // Alternating endpoint fragments cannot mix framing or escaped strings.
    JsonAssembler usb,ble;
    const std::string a="{\"id\":1,\"method\":\"device.status\",\"x\":\"brace } and \\\"\"}";
    const std::string b="{\"id\":2,\"method\":\"v.oai.thstatus\",\"params\":[{\"id\":0}]}";
    for (size_t i=0;i<a.size() || i<b.size();++i) {
      if (i<a.size()) assert(usb.feed(a[i])==(i+1==a.size() ? Assembly::Complete : Assembly::Pending));
      if (i<b.size()) assert(ble.feed(b[i])==(i+1==b.size() ? Assembly::Complete : Assembly::Pending));
    }
    assert(std::string(usb.data(),usb.size())==a && std::string(ble.data(),ble.size())==b); ++tests;
  }
  { // A malformed/overflowed BLE stream cannot clear a partial USB request.
    JsonAssembler usb,ble; assert(usb.feed('{')==Assembly::Pending);
    assert(ble.feed('{')==Assembly::Pending);
    for (size_t i=1;i<JsonAssembler::capacity-1;++i) assert(ble.feed(' ')==Assembly::Pending);
    assert(ble.feed(' ')==Assembly::Overflow && ble.size()==0);
    assert(usb.feed('}')==Assembly::Complete && std::string(usb.data())=="{}"); ++tests;
  }
  { // Reconnect resets only that endpoint's parser, including quote state.
    JsonAssembler usb,ble;
    for (char c:std::string("{\"text\":\"partial")) ble.feed(c);
    usb.feed('{'); ble.reset();
    assert(ble.feed('{')==Assembly::Pending); assert(ble.feed('}')==Assembly::Complete);
    assert(usb.feed('}')==Assembly::Complete); ++tests;
  }
  { // Reply fragments and cancellation are independent between endpoints.
    Queue usb,ble; Message completed;
    const std::string usbReply(120,'u'),bleReply(100,'b');
    assert(usb.enqueue(usbReply.data(),usbReply.size(),{},0));
    assert(ble.enqueue(bleReply.data(),bleReply.size(),{},0));
    usb.next(0); assert(!usb.submitted(61,completed));
    ble.next(0); assert(!ble.submitted(61,completed));
    usb.disconnect(); assert(usb.next(0)==nullptr);
    assert(ble.next(0)->offset==61 && ble.submitted(40,completed));
    assert(std::string(completed.bytes,completed.length)==bleReply+"\n"); ++tests;
  }
  { // A held BLE press is cleaned only on BLE, then USB begins with no old
    // gesture. No queue migration or synthesized release reaches the new link.
    Queue usb,ble; Selection s; Message completed;
    const char* down="{\"m\":\"v.oai.hid\",\"p\":{\"k\":\"ACT06\",\"act\":1}}";
    assert(ble.enqueue(down,strlen(down),{Kind::Key,6,1,0},0));
    ble.next(0); assert(ble.submitted(strlen(down)+1,completed));
    s.update(false,true,false,0); s.update(true,true,ble.heldKeys()!=0,1);
    assert(s.beginCleanup()); ble.cancelInputs();
    assert(usb.next(1)==nullptr && ble.next(1)->tag.action==0);
    assert(ble.submitted(ble.next(1)->length,completed));
    s.update(true,true,ble.recoveryPending(),2);
    assert(s.active()==Link::Usb && usb.next(2)==nullptr && !ble.heldKeys()); ++tests;
  }
  { // Active-link changes and same-link reconnects invalidate captured touch;
    // stable polls and an inactive endpoint's connection do not.
    SessionGeneration generation;
    generation.update(Link::None,0); assert(generation.value()==0);
    generation.update(Link::Bluetooth,1); assert(generation.value()==1);
    generation.update(Link::Bluetooth,1); assert(generation.value()==1);
    generation.update(Link::Bluetooth,2); assert(generation.value()==2);
    generation.update(Link::Usb,1); assert(generation.value()==3);
    generation.update(Link::Usb,1); assert(generation.value()==3);
    generation.update(Link::Usb,2); assert(generation.value()==4); ++tests;
  }
  { // A cleanup handoff keeps the old generation until selection changes;
    // the caller can then suppress held joystick/encoder until finger lift.
    Selection s; SessionGeneration generation;
    s.update(false,true,false,0); generation.update(s.active(),1);
    const auto original=generation.value();
    s.update(true,true,true,1); generation.update(s.active(),1);
    assert(generation.value()==original);
    s.update(true,true,false,2); generation.update(s.active(),1);
    assert(generation.value()==original+1);
    s.update(false,false,false,3); generation.update(s.active(),0);
    assert(generation.value()==original+2); ++tests;
  }
  std::cout<<tests<<"/14 connection tests passed\n";
  assert(tests==14);
}
