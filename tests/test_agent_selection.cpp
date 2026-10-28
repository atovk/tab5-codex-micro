#include "agent_selection.h"
#include "micro_transport.h"

#include <cassert>
#include <cstring>
#include <iostream>

HostFeedback::Chat liveChat() {
  HostFeedback::Chat chat;
  chat.fresh=true;
  chat.source=HostFeedback::ChatSource::DesktopIpc;
  chat.speed=HostFeedback::Speed::Fast;
  chat.mode=HostFeedback::Mode::Plan;
  chat.permission=HostFeedback::Permission::FullAccess;
  std::strcpy(chat.thread_id,"01234567-89ab-cdef-0123-456789abcdef");
  std::strcpy(chat.title,"Previously tracked chat");
  return chat;
}

void cleared(const HostFeedback::Chat& chat, bool available) {
  assert(!available && !chat.fresh);
  assert(chat.source==HostFeedback::ChatSource::Unavailable);
  assert(chat.speed==HostFeedback::Speed::Unknown);
  assert(chat.mode==HostFeedback::Mode::Unknown);
  assert(chat.permission==HostFeedback::Permission::Unknown);
  assert(!chat.thread_id[0] && !chat.title[0]);
}

int main() {
  unsigned checks=0;
  {
    auto chat=liveChat(); bool available=true;
    AgentSelection::filterExplicitTarget(-1,chat,available);
    assert(available && chat.fresh && chat.title[0]);
    ++checks;
  }
  {
    int8_t selected=-1;
    for (int index=0;index<6;++index) {
      assert(AgentSelection::submitted(selected,index,1,false));
      assert(selected==index);
      auto chat=liveChat(); bool available=true;
      AgentSelection::filterExplicitTarget(selected,chat,available);
      cleared(chat,available);
    }
    ++checks;
  }
  {
    int8_t selected=-1;
    for (int key : {-1,6,10,12,13,14,15})
      for (uint8_t action : {uint8_t(0),uint8_t(1),uint8_t(2)}) {
        assert(!AgentSelection::submitted(selected,key,action,false));
        assert(selected==-1);
      }
    for (int key=0;key<6;++key)
      for (uint8_t action : {uint8_t(0),uint8_t(2)}) {
        assert(!AgentSelection::submitted(selected,key,action,false));
        assert(selected==-1);
      }
    ++checks;
  }
  {
    // Capture-only input and framebuffer diagnostics cannot select an Agent.
    int8_t selected=-1;
    for (int key=0;key<6;++key) {
      assert(!AgentSelection::submitted(selected,key,1,true));
      assert(selected==-1);
    }
    assert(AgentSelection::submitted(selected,2,1,false));
    assert(!AgentSelection::submitted(selected,5,1,true));
    assert(selected==2);
    ++checks;
  }
  {
    // An old companion can keep emitting valid fixed-target metadata. None of
    // those packets may resurrect its title or settings after a slot press.
    int8_t selected=-1;
    assert(AgentSelection::submitted(selected,4,1,false));
    for (unsigned heartbeat=0;heartbeat<100;++heartbeat) {
      auto chat=liveChat(); bool available=true;
      AgentSelection::filterExplicitTarget(selected,chat,available);
      cleared(chat,available);
      assert(selected==4);
    }
    ++checks;
  }
  {
    // Queue ownership supplies this transition only for a fully submitted
    // native key message, not a failed enqueue or a partial transport write.
    using namespace MicroTransport;
    Queue queue; Message completed;
    int8_t selected=-1;
    const char* press="{\"m\":\"v.oai.hid\",\"p\":{\"k\":\"AG01\",\"act\":1}}";
    assert(queue.enqueue(press,std::strlen(press),{Kind::Key,1,1,0},0));
    assert(selected==-1);
    queue.next(0);
    assert(!queue.submitted(10,completed));
    assert(selected==-1);
    queue.next(1);
    assert(queue.submitted(std::strlen(press)+1-10,completed));
    assert(AgentSelection::submitted(selected,completed.tag.key,completed.tag.action,false));
    assert(selected==1);
    ++checks;
  }
  std::cout<<checks<<"/6 Agent selection tests passed\n";
  assert(checks==6);
}
