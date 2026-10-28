#include "micro_transport.h"
#include <assert.h>
#include <string>
#include <iostream>

using namespace MicroTransport;
static const Tag press{Kind::Key,6,1,0}, release{Kind::Key,6,0,0};
static const Tag turn{Kind::Key,14,2,0};
static const char* down="{\"m\":\"v.oai.hid\",\"p\":{\"k\":\"ACT06\",\"act\":1}}";
static const char* up="{\"m\":\"v.oai.hid\",\"p\":{\"k\":\"ACT06\",\"act\":0}}";
static bool push(Queue& q,const char* text,Tag tag,uint32_t at=0) {
  return q.enqueue(text,strlen(text),tag,at);
}
static Message finish(Queue& q,uint32_t now=0) {
  Message completed;
  for (;;) {
    const auto* next=q.next(now); assert(next);
    const size_t remaining=next->length-next->offset;
    if (q.submitted(remaining>61 ? 61 : remaining,completed)) return completed;
  }
}
int main() {
  unsigned tests=0;
  { // No report is consumed when the endpoint refuses it; retry exact bytes.
    Queue q; assert(push(q,down,press));
    const std::string first(q.next(0)->bytes,q.next(0)->length);
    q.failed(); q.failed();
    assert(q.next(50)->offset==0 && first==std::string(q.next(50)->bytes,q.next(50)->length));
    assert(q.stats().failed==2 && q.heldKeys()==0);
    auto done=finish(q,50); assert(done.tag.action==1 && q.heldKeys()==(1u<<6));
    ++tests;
  }
  { // Press/release/press remain in order even across failed submissions.
    Queue q; assert(push(q,down,press)); assert(push(q,up,release)); assert(push(q,down,press));
    assert(finish(q).tag.action==1); q.failed();
    assert(finish(q).tag.action==0); assert(finish(q).tag.action==1);
    assert(q.size()==0); ++tests;
  }
  { // A multi-report JSON response cannot interleave with inputs or cleanup.
    Queue q; assert(push(q,down,press)); finish(q);
    const std::string response(150,'x'); assert(q.enqueue(response.data(),response.size(),{},0));
    Message done; assert(q.next(0)->offset==0); assert(!q.submitted(61,done));
    q.cancelInputs();
    assert(q.next(1)->tag.kind==Kind::Response && q.next(1)->offset==61);
    assert(!q.submitted(61,done)); assert(q.next(1)->offset==122);
    assert(q.submitted(29,done)); assert(done.tag.kind==Kind::Response);
    assert(finish(q).tag.action==0 && q.heldKeys()==0); ++tests;
  }
  { // Stale unsent gestures disappear; a submitted press receives a release.
    Queue q; assert(push(q,down,press)); finish(q);
    assert(push(q,"turn",turn,0)); assert(push(q,up,release,0));
    const auto* next=q.next(251);
    assert(next && next->tag.action==0 && next->tag.key==6);
    assert(!push(q,down,press,251));
    auto done=finish(q,251); assert(strstr(done.bytes,"\"act\":0"));
    assert(q.size()==0 && q.heldKeys()==0 && q.stats().stale==1);
    assert(push(q,down,press,252)); ++tests;
  }
  { // Disconnect drops both queued actions and recovery, never replays old up.
    Queue q; assert(push(q,down,press)); finish(q); assert(push(q,up,release));
    q.cancelInputs(); assert(q.next(0)); q.failed(); q.disconnect();
    assert(q.next(1)==nullptr && !q.heldKeys() && !q.recoveryPending());
    assert(push(q,down,press,2)); assert(finish(q,2).tag.action==1); ++tests;
  }
  { // A full queue cannot strand a submitted press. Cleanup needs no slot.
    Queue q; assert(push(q,down,press)); finish(q);
    for (unsigned i=0;i<Queue::capacity;++i) assert(push(q,"reply",{},0));
    assert(!push(q,up,release)); assert(q.size()==Queue::capacity);
    assert(finish(q).tag.action==0 && !q.recoveryPending());
    assert(q.size()==Queue::capacity); ++tests;
  }
  { // Only continuous motion in the same action zone may be coalesced.
    Queue q;
    assert(push(q,"east1",{Kind::Stick,-1,0,2}));
    assert(push(q,"east2",{Kind::Stick,-1,0,2},1));
    assert(q.size()==1 && q.stats().coalesced==1);
    assert(push(q,"south",{Kind::Stick,-1,0,3},2));
    assert(push(q,"weak",{Kind::Stick,-1,0,1},3));
    assert(push(q,"neutral",{Kind::Stick,-1,0,0},4));
    assert(push(q,"east3",{Kind::Stick,-1,0,2},5));
    assert(q.size()==5);
    assert(std::string(finish(q,5).bytes)=="east2\n");
    assert(finish(q,5).tag.stickZone==3); assert(finish(q,5).tag.stickZone==1);
    assert(finish(q,5).tag.stickZone==0); assert(finish(q,5).tag.stickZone==2);
    ++tests;
  }
  { // Submitted joystick movement always gets neutral after same-session loss.
    Queue q; assert(push(q,"move",{Kind::Stick,-1,0,2})); finish(q);
    q.cancelInputs(); assert(q.stickActive() && q.recoveryPending());
    auto done=finish(q); assert(done.tag.kind==Kind::Stick && !done.tag.stickZone);
    assert(strstr(done.bytes,"\"a\":0,\"d\":0")); assert(!q.stickActive()); ++tests;
  }
  { // Capture sends no native event, including an already pending recovery.
    Queue q; assert(push(q,down,press)); finish(q); q.cancelInputs();
    q.next(0); q.failed(); // cleanup exists but has not been submitted
    q.suspendInputs(true); assert(q.next(0)==nullptr);
    assert(!push(q,down,press)); assert(push(q,"reply",{}));
    assert(finish(q).tag.kind==Kind::Response); assert(q.heldKeys());
    assert(q.next(0)==nullptr); q.suspendInputs(false);
    assert(finish(q).tag.action==0); assert(q.next(0)==nullptr); ++tests;
  }
  { // Unsubmitted captured presses never escape after capture ends.
    Queue q; assert(push(q,down,press)); q.suspendInputs(true);
    assert(!push(q,up,release)); q.suspendInputs(false);
    assert(q.next(0)==nullptr && !q.recoveryPending()); ++tests;
  }
  { // Lifetime arithmetic survives millis() wraparound.
    Queue q; assert(push(q,down,press,0xfffffff0u));
    assert(q.next(20)); assert(q.next(300)==nullptr); ++tests;
  }
  { // A release without a corresponding press can trigger a desktop command,
    // so discard it after reconnect or a diagnostic finger-lift handoff.
    Queue q; assert(!push(q,up,release));
    assert(push(q,down,press)); assert(push(q,up,release));
    assert(!push(q,up,release)); finish(q); finish(q);
    assert(!push(q,up,release)); q.disconnect();
    assert(!push(q,up,release)); ++tests;
  }
  { // Native messages cannot be partially serialized and then cancelled.
    Queue q; const std::string large(61,'x');
    assert(!q.enqueue(large.data(),large.size(),press,0));
    assert(q.enqueue(large.data(),large.size(),{},0)); ++tests;
  }
  { // SCREEN's deferred lines remain whole, bounded, and ordered after partial
    // CDC writes. Overflow cannot corrupt existing lines or the frame bytes.
    DeferredLines lines; assert(lines.data()==nullptr);
    for (unsigned i=0;i<DeferredLines::capacity;++i) assert(lines.push("INPUT_EVENT {}",14));
    assert(!lines.push("overflow",8)); assert(lines.dropped()==1);
    std::string output;
    while (lines.size()) {
      const auto count=lines.remaining()>3 ? 3 : lines.remaining();
      output.append(lines.data(),count); lines.consumed(count);
    }
    std::string expected; for (unsigned i=0;i<DeferredLines::capacity;++i) expected+="INPUT_EVENT {}\n";
    assert(output==expected && !lines.size()); ++tests;
  }
  { // Zone classification preserves the desktop action threshold/direction.
    assert(stickZone(0,0)==0 && stickZone(.4f,.49f)==1);
    assert(stickZone(0,.5f)==2 && stickZone(.25f,1)==3);
    assert(stickZone(.5f,1)==4 && stickZone(.75f,1)==5 && stickZone(.99f,1)==2);
    assert(keyIndex("ENC")==13 && keyIndex("ACT12")==12 && keyIndex("bad")==-1);
    ++tests;
  }
  { // Do not show a click success after only its press was submitted. A new
    // gesture also clears the previous click's still-visible success receipt.
    KeyReceipts receipts;
    assert(receipts.begin(6)==Receipt::Clear);
    assert(receipts.submitted(6,1)==Receipt::Clear);
    assert(receipts.submitted(6,0)==Receipt::Submitted);
    assert(receipts.begin(6)==Receipt::Clear);
    assert(receipts.failed(6)==Receipt::NotSent);
    assert(receipts.submitted(6,0)==Receipt::NotSent);
    ++tests;
  }
  { // An accepted press/release pair can expire before USB accepts either.
    // Cancellation exposes the exact affected key once, so poll shows failure.
    Queue q; KeyReceipts receipts;
    assert(receipts.begin(6)==Receipt::Clear);
    assert(push(q,down,press)); assert(push(q,up,release));
    assert(q.takeDroppedKeys()==0); assert(q.next(251)==nullptr);
    const auto dropped=q.takeDroppedKeys(); assert(dropped==(1u<<6));
    assert(receipts.failed(6)==Receipt::NotSent); assert(q.takeDroppedKeys()==0);
    assert(!q.recoveryPending()); ++tests;
  }
  { // Release timeout is a failed gesture, even if its safety cleanup later
    // succeeds. A subsequent genuine press/release can establish success again.
    Queue q; KeyReceipts receipts;
    receipts.begin(6); assert(push(q,down,press));
    auto done=finish(q); assert(receipts.submitted(done.tag.key,done.tag.action)==Receipt::Clear);
    assert(push(q,up,release)); assert(q.next(251));
    assert(q.takeDroppedKeys()==(1u<<6)); assert(receipts.failed(6)==Receipt::NotSent);
    done=finish(q,251); assert(receipts.submitted(done.tag.key,done.tag.action)==Receipt::NotSent);
    receipts.begin(6); assert(push(q,down,press,252)); assert(push(q,up,release,252));
    done=finish(q,252); assert(receipts.submitted(done.tag.key,done.tag.action)==Receipt::Clear);
    done=finish(q,252); assert(receipts.submitted(done.tag.key,done.tag.action)==Receipt::Submitted);
    ++tests;
  }
  { // Full-queue cancellation reports each queued or already-held key, while
    // reconnect discards old receipt notifications along with the old actions.
    Queue q; assert(push(q,down,press)); finish(q);
    assert(push(q,"ACT07 press",{Kind::Key,7,1,0}));
    for (unsigned i=1;i<Queue::capacity;++i) assert(push(q,"turn",turn));
    assert(!push(q,up,release));
    assert(q.takeDroppedKeys()==((1u<<6)|(1u<<7)));
    q.cancelInputs(); assert(q.takeDroppedKeys()==(1u<<6));
    q.cancelInputs(); q.disconnect(); assert(q.takeDroppedKeys()==0);
    ++tests;
  }
  std::cout<<tests<<"/19 transport tests passed\n";
  assert(tests==19);
}
