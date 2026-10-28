#include "micro_ble_diagnostics.h"
#include <cassert>
#include <iostream>
using namespace MicroBleDiagnostics;
Context ready() {
  Context c; c.session=7; c.transportGeneration=4;
  c.connected=c.authenticated=c.bonded=c.ready=c.mappingKnown=c.mappingSafe=true;
  c.fastKey=6; return c;
}
int main() {
  unsigned tests=0; Step step;
  { Schedule s; auto c=ready(); assert(s.arm(Plan::FastPair,1000,0,c));
    assert(!s.next(999,c,step) && !s.next(1000,c,step));
    c.activeBle=true; assert(s.next(1001,c,step));
    assert(step.kind==Kind::FastKey && step.action==1 && step.fastKey==6 && step.session==7);
    assert(!s.next(1002,c,step)); assert(s.completeStep(true));
    assert(!s.next(1100,c,step)); assert(s.next(1101,c,step) && step.action==0); assert(s.completeStep(true));
    assert(!s.next(4100,c,step)); assert(s.next(4101,c,step) && step.action==1); assert(s.completeStep(true));
    assert(s.next(4201,c,step) && step.action==0); assert(s.completeStep(true));
    assert(s.state()==State::Complete && s.acceptedSteps()==4 && !s.takeCleanupRequired());
    assert(!s.next(5000,c,step)); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true;
    assert(s.arm(Plan::DialPair,1000,0,c)); assert(s.next(1000,c,step));
    assert(step.kind==Kind::DialClockwise && step.action==2); s.completeStep(true);
    assert(!s.next(2999,c,step)); assert(s.next(3000,c,step));
    assert(step.kind==Kind::DialCounterclockwise && step.action==2); s.completeStep(true);
    assert(s.state()==State::Complete && s.acceptedSteps()==2); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true; s.arm(Plan::PlanPair,1000,0,c);
    const Kind expected[]={Kind::StickUp,Kind::StickNeutral,Kind::StickUp,Kind::StickNeutral};
    const uint32_t at[]={1000,1100,4100,4200};
    for (unsigned i=0;i<4;++i) { assert(s.next(at[i],c,step)); assert(step.kind==expected[i]); s.completeStep(true); }
    assert(s.state()==State::Complete); ++tests; }
  { Schedule s; auto c=ready(); c.mappingSafe=false;
    assert(!s.arm(Plan::FastPair,1000,0,c)); c.mappingSafe=true; c.fastKey=5;
    assert(!s.arm(Plan::FastPair,1000,0,c)); c.fastKey=13;
    assert(!s.arm(Plan::FastPair,1000,0,c)); c.fastKey=6;
    assert(!s.arm(Plan(99),1000,0,c)); ++tests; }
  { Schedule s; auto c=ready(); assert(!s.arm(Plan::FastPair,999,0,c));
    assert(!s.arm(Plan::FastPair,30001,0,c)); c.suppressed=true;
    assert(!s.arm(Plan::FastPair,1000,0,c)); c.suppressed=false; c.session=0;
    assert(!s.arm(Plan::FastPair,1000,0,c)); ++tests; }
  { Schedule s; auto c=ready(); c.ready=false;
    assert(s.arm(Plan::DialPair,1000,100,c)); // USB still owns input; HID is not subscribed yet.
    assert(!s.next(600,c,step) && s.state()==State::Armed && s.acceptedSteps()==0);
    c.mappingKnown=false; // USB removal invalidates metadata until BLE resynchronizes.
    assert(!s.next(1100,c,step) && s.state()==State::Armed && s.acceptedSteps()==0);
    c.mappingKnown=true; c.activeBle=true;
    assert(!s.next(4099,c,step) && s.state()==State::Armed && s.acceptedSteps()==0);
    c.ready=true;
    assert(s.next(4100,c,step) && step.kind==Kind::DialClockwise && step.session==7);
    assert(s.completeStep(true));
    assert(!s.next(6099,c,step));
    assert(s.next(6100,c,step) && step.kind==Kind::DialCounterclockwise && step.session==7);
    assert(s.completeStep(true));
    assert(s.state()==State::Complete && s.acceptedSteps()==2 && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.ready=false; c.activeBle=true;
    assert(s.arm(Plan::FastPair,1000,0,c));
    assert(!s.next(4000,c,step) && s.state()==State::Armed && s.acceptedSteps()==0);
    assert(!s.next(4001,c,step) && s.state()==State::Aborted && s.reason()==Reason::StartTimeout);
    assert(s.acceptedSteps()==0 && !s.takeCleanupRequired());
    c.ready=true; assert(!s.next(4002,c,step)); ++tests; }
  { Schedule s; auto c=ready(); c.authenticated=false;
    assert(c.ready && c.bonded); // A stale ready flag must not authorize an unauthenticated session.
    assert(!s.arm(Plan::FastPair,1000,0,c));
    assert(s.state()==State::Aborted && s.reason()==Reason::InvalidRequest); ++tests; }
  { Schedule s; auto c=ready(); c.bonded=false;
    assert(c.ready && c.authenticated);
    assert(!s.arm(Plan::FastPair,1000,0,c));
    assert(s.state()==State::Aborted && s.reason()==Reason::InvalidRequest); ++tests; }
  { Schedule s; auto c=ready(); c.ready=false;
    assert(s.arm(Plan::FastPair,1000,0,c)); c.authenticated=false;
    assert(!s.next(1,c,step) && s.state()==State::Aborted && s.reason()==Reason::Disconnected);
    assert(s.acceptedSteps()==0 && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.ready=false;
    assert(s.arm(Plan::FastPair,1000,0,c)); c.bonded=false;
    assert(!s.next(1,c,step) && s.state()==State::Aborted && s.reason()==Reason::Disconnected);
    assert(s.acceptedSteps()==0 && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.ready=false;
    assert(s.arm(Plan::FastPair,1000,0,c));
    assert(!s.next(500,c,step) && s.state()==State::Armed);
    ++c.session; c.ready=true; c.activeBle=true;
    assert(!s.next(1000,c,step) && s.state()==State::Aborted && s.reason()==Reason::SessionChanged);
    assert(s.session()==7 && s.acceptedSteps()==0 && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true;
    assert(s.arm(Plan::FastPair,1000,0,c));
    assert(s.next(1000,c,step) && step.action==1); assert(s.completeStep(true));
    c.authenticated=false; // Even a stale ready=true cannot keep a running test alive.
    assert(!s.next(1100,c,step) && s.state()==State::Aborted && s.reason()==Reason::Disconnected);
    assert(s.acceptedSteps()==1 && s.takeCleanupRequired() && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true;
    assert(s.arm(Plan::FastPair,1000,0,c));
    assert(s.next(1000,c,step) && step.action==1); assert(s.completeStep(true));
    c.bonded=false;
    assert(!s.next(1100,c,step) && s.state()==State::Aborted && s.reason()==Reason::Disconnected);
    assert(s.acceptedSteps()==1 && s.takeCleanupRequired() && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true;
    assert(s.arm(Plan::FastPair,1000,0,c));
    assert(s.next(1000,c,step) && step.action==1); assert(s.completeStep(true));
    c.ready=false;
    assert(!s.next(1100,c,step) && s.state()==State::Aborted && s.reason()==Reason::Disconnected);
    assert(s.acceptedSteps()==1 && s.takeCleanupRequired() && !s.takeCleanupRequired());
    c.ready=true; assert(!s.next(1200,c,step)); ++tests; }
  { Schedule s; auto c=ready(); assert(s.arm(Plan::FastPair,1000,0,c));
    assert(!s.arm(Plan::DialPair,1000,0,c)); assert(s.session()==7); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::FastPair,1000,0,c); ++c.session;
    assert(!s.next(1,c,step)); assert(s.reason()==Reason::SessionChanged && !s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::FastPair,1000,0,c); c.ready=false; c.connected=false;
    assert(!s.next(1,c,step) && s.reason()==Reason::Disconnected); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::FastPair,1000,0,c); c.mappingSafe=false;
    assert(!s.next(1,c,step) && s.reason()==Reason::MappingChanged); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::FastPair,1000,0,c); c.fastKey=7;
    assert(!s.next(1000,c,step) && s.reason()==Reason::MappingChanged); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::FastPair,1000,0,c);
    assert(!s.next(4001,c,step) && s.reason()==Reason::StartTimeout); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true; s.arm(Plan::FastPair,1000,0,c);
    assert(s.next(1000,c,step)); s.completeStep(true); ++c.transportGeneration;
    assert(!s.next(1100,c,step) && s.reason()==Reason::TransportChanged);
    assert(!s.arm(Plan::FastPair,1000,1100,c)); assert(s.takeCleanupRequired());
    assert(!s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true; s.arm(Plan::FastPair,1000,0,c);
    s.next(1000,c,step); s.completeStep(true); c.activeBle=false;
    assert(!s.next(1100,c,step) && s.reason()==Reason::TransportChanged && s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true; s.arm(Plan::FastPair,1000,0,c);
    s.next(1000,c,step); assert(!s.completeStep(false));
    assert(s.reason()==Reason::SubmitFailed && s.takeCleanupRequired());
    assert(!s.next(1100,c,step)); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true; s.arm(Plan::FastPair,1000,0,c);
    s.next(1000,c,step); // Integration forgot completeStep: no duplicate, bounded abort.
    assert(!s.next(1001,c,step)); assert(!s.next(11001,c,step));
    assert(s.reason()==Reason::Deadline && s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true; s.arm(Plan::FastPair,1000,0,c);
    s.next(1000,c,step); s.completeStep(true); assert(s.next(5000,c,step));
    assert(step.action==0); s.completeStep(true);
    assert(!s.next(5000,c,step)); // No catch-up press/release burst.
    c.suppressed=true; assert(!s.next(5001,c,step) && s.reason()==Reason::Suppressed); ++tests; }
  { Schedule s; auto c=ready(); c.activeBle=true;
    assert(s.arm(Plan::DialPair,1000,0xffffff00u,c));
    assert(!s.next(uint32_t(0xffffff00u+999),c,step));
    assert(s.next(uint32_t(0xffffff00u+1000),c,step)); s.completeStep(true);
    assert(s.next(uint32_t(0xffffff00u+3000),c,step)); s.completeStep(true);
    assert(s.state()==State::Complete); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::FastPair,1000,0,c); s.cancel();
    assert(s.reason()==Reason::Cancelled && !s.takeCleanupRequired());
    c.activeBle=true; s.arm(Plan::FastPair,1000,0,c); s.next(1000,c,step); s.completeStep(true); s.cancel();
    assert(s.takeCleanupRequired() && !s.next(1100,c,step)); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::PlanPair,1000,0,c);
    c.mappingKnown=false; assert(!s.next(1500,c,step) && s.state()==State::Armed);
    c.mappingKnown=true; c.activeBle=true; assert(s.next(1600,c,step)); s.completeStep(true);
    c.mappingKnown=false; assert(!s.next(1700,c,step) && s.reason()==Reason::MappingChanged);
    assert(s.takeCleanupRequired()); ++tests; }
  { Schedule s; auto c=ready(); s.arm(Plan::PlanPair,1000,0,c);
    c.mappingKnown=false; assert(!s.next(4001,c,step) && s.reason()==Reason::StartTimeout); ++tests; }
  std::cout<<tests<<"/30 BLE diagnostic schedule tests passed\n";
  assert(tests==30);
}
