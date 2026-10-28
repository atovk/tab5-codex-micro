"""Exercise production input-policy invalidation without rendering a frame."""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class InputPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "src/micro_ui.cpp").read_text()
        policy = source[source.index("void syncInputPolicy("):source.index("\nbool render(")]
        # Keep both policy and its retained state from production. The renderer
        # is deliberately absent: an input-policy update must work on its own.
        retained = "\n".join(
            re.search(rf"^.*\b{name}\b.*;$", source, re.MULTILINE).group(0)
            for name in ("inputActionMappings", "inputActionTransport")
        )
        harness = r'''
#include "micro_ui.h"
#include "micro_controls.h"
#include "control_layout.h"
#include <algorithm>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#define portENTER_CRITICAL(p) (void)(p)
#define portEXIT_CRITICAL(p) (void)(p)
namespace MicroUI {
int inputMux;
''' + retained + "\n" + policy + r'''
}
using MicroUI::State;
using MicroUI::Transport;
using Action = HostFeedback::Action;
using Events = std::vector<MicroUI::Event>;

void require(bool value, const char* reason) {
  if (!value) throw std::runtime_error(reason);
}
Events drain() {
  Events result;
  MicroUI::Event event;
  while (MicroControls::nextEvent(event)) result.push_back(event);
  return result;
}
Events sample(int x, int y, bool down, uint32_t now) {
  MicroControls::sample(x,y,down,now);
  return drain();
}
void one(const Events& events, const char* key, uint8_t action) {
  require(events.size()==1,"expected exactly one native edge");
  require(events[0].key && std::strcmp(events[0].key,key)==0
      && events[0].action==action && !events[0].joystick,"incorrect native edge");
}
void pair(const Events& events, const char* key) {
  require(events.size()==2,"expected one balanced native pair");
  one({events[0]},key,1); one({events[1]},key,0);
}
State baseline() {
  MicroControls::reset();
  std::fill_n(MicroUI::inputActionMappings,7,Action::Unknown);
  MicroUI::inputActionTransport=Transport::None;
  State state{};
  state.connected=true;
  state.usbMounted=true;
  state.activeTransport=Transport::Usb;
  state.hostConfig.fresh=true;
  const Action actions[]={Action::Fast,Action::Approve,Action::Reject,
      Action::Fork,Action::Voice,Action::None,Action::Send};
  std::copy_n(actions,7,state.hostConfig.actions);
  MicroUI::syncInputPolicy(state);
  require(drain().empty(),"initial policy update emitted input");
  return state;
}
void arm(int x=265) {
  require(sample(x,617,true,1000).empty(),"deferred key emitted on touch-down");
  require(MicroControls::visual().held>=0,"pending press lacks visual feedback");
}
void canceledRelease(const State& original, int x=265) {
  require(drain().empty(),"policy invalidation emitted input");
  require(MicroControls::visual().held==-1,"policy invalidation retained pressed state");
  // Even an immediate recovery before release must not revive the old touch.
  MicroUI::syncInputPolicy(original);
  require(sample(x,617,false,1020).empty(),"canceled touch was submitted without rendering");
  arm(x);
  pair(sample(x,617,false,1040),x==265 ? "ACT07" : "ACT12");
}
void freshness() {
  for (int reason=0;reason<2;++reason) {
    const auto original=baseline();
    arm();
    auto changed=original;
    if (reason==0) changed.hostConfig.fresh=false;
    else changed.connected=false;
    MicroUI::syncInputPolicy(changed);
    canceledRelease(original);
  }
}
void remapSameMask() {
  for (int target : {1,6}) {
    const auto original=baseline();
    const int x=target==1 ? 265 : 1140;
    arm(x);
    auto changed=original;
    // Approve -> Reject and Send -> Approve keep the release-action mask
    // identical, so comparing the mask alone cannot catch this remapping.
    changed.hostConfig.actions[target]=target==1 ? Action::Reject : Action::Approve;
    MicroUI::syncInputPolicy(changed);
    canceledRelease(original,x);
  }
}
void transport() {
  for (auto next : {Transport::Bluetooth,Transport::None}) {
    const auto original=baseline();
    arm();
    auto changed=original;
    changed.activeTransport=next;
    MicroUI::syncInputPolicy(changed);
    canceledRelease(original);
  }
}
void unchanged() {
  auto state=baseline();
  arm();
  for (uint32_t now : {1020,1500,5000}) {
    // Fresh metadata updates that do not change action policy are harmless.
    state.hostChat.fresh=!state.hostChat.fresh;
    ++state.rpc_count;
    MicroUI::syncInputPolicy(state);
    require(sample(265,617,true,now).empty(),"unchanged policy submitted a held key");
  }
  pair(sample(265,617,false,5100),"ACT07");
}
void nativeHold() {
  struct Key { int x,y; const char* name; };
  for (const Key key : {Key{ControlLayout::DialX,ControlLayout::DialY,"ENC"},
                        Key{761,617,"ACT10"}}) {
    auto state=baseline();
    one(sample(key.x,key.y,true,1000),key.name,1);
    state.hostConfig.fresh=false;
    MicroUI::syncInputPolicy(state);
    require(sample(key.x,key.y,true,1500).empty(),"freshness loss interrupted native hold");
    state.hostConfig.fresh=true;
    state.activeTransport=Transport::Bluetooth;
    state.hostConfig.actions[4]=Action::Send;
    MicroUI::syncInputPolicy(state);
    require(sample(key.x,key.y,true,5000).empty(),"mapping update repeated native down");
    if (std::strcmp(key.name,"ENC")==0)
      require(MicroControls::visual().pressStarted==1000,"encoder hold origin changed");
    one(sample(key.x,key.y,false,5100),key.name,0);
    require(sample(key.x,key.y,false,5110).empty(),"native release was duplicated");
  }
}
int main(int argc,char** argv) {
  try {
    require(argc==2,"expected a test case");
    const std::string test=argv[1];
    if (test=="freshness") freshness();
    else if (test=="same-mask-remap") remapSameMask();
    else if (test=="transport") transport();
    else if (test=="unchanged") unchanged();
    else if (test=="native-hold") nativeHold();
    else throw std::runtime_error("unknown test case");
    std::cout << test << " passed without rendering\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
'''
        cls.directory = tempfile.TemporaryDirectory(prefix="tab5-input-policy-")
        cls.addClassCleanup(cls.directory.cleanup)
        cpp = Path(cls.directory.name) / "test.cpp"
        cls.binary = Path(cls.directory.name) / "test"
        cpp.write_text(harness)
        build = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Iinclude",
             str(cpp), "src/micro_controls.cpp", "-o", str(cls.binary)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("passed without rendering", result.stdout)

    def test_freshness_or_connection_loss_cancels_before_render(self):
        self.run_case("freshness")

    def test_remapping_with_unchanged_mask_cancels_before_render(self):
        self.run_case("same-mask-remap")

    def test_transport_change_cancels_before_render(self):
        self.run_case("transport")

    def test_unchanged_policy_allows_one_balanced_release(self):
        self.run_case("unchanged")

    def test_voice_and_encoder_keep_native_hold_edges(self):
        self.run_case("native-hold")


if __name__ == "__main__":
    unittest.main()
