"""Run the production Bluetooth settings touch path without Arduino or radios."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which('c++')


@unittest.skipUnless(COMPILER, 'host C++ compiler is unavailable')
class BluetoothUiTests(unittest.TestCase):
    def test_local_actions_require_correct_state_and_fresh_confirmation_touch(self):
        source = (ROOT / 'src/micro_ui.cpp').read_text()
        state = source[source.index('struct InputState {'):source.index('portMUX_TYPE inputMux')]
        touch = source[source.index('Event touch(int16_t'):source.index('\nbool pollEvent(Event&')]
        labels = source[source.index('const char* bluetoothStatus('):source.index('\nvoid drawBluetoothEntry(')]
        harness = r'''
#include <cassert>
#include <cstring>
#include <cstdio>
#include <iostream>
#include "micro_ui.h"
#include "ui_locale.h"
#define portENTER_CRITICAL(p) (void)(p)
#define portEXIT_CRITICAL(p) (void)(p)
uint32_t millis() { return 0; }
namespace MicroControls {
struct Visual { int held=-1; };
Visual view;
unsigned nativeSamples=0;
void cancel() { view.held=-1; }
const Visual& visual() { return view; }
void nextEvent(MicroUI::Event&) {}
void sample(int,int,bool,uint32_t) { ++nativeSamples; }
}
namespace MicroGames {
void setActive(bool) {}
void touch(int,int,bool) {}
}
namespace MicroUI {
constexpr int W=1280,H=720;
''' + state + r'''
InputState input;
int inputMux;
bool inside(int x,int y,int left,int top,int width,int height) {
  return x>=left && y>=top && x<left+width && y<top+height;
}
uint8_t sliderValue(int,uint8_t minimum) { return minimum+30; }
const char* tr(const char* zh,const char* en) { return UiLocale::text(zh,en); }
''' + labels + touch + r'''
}
using namespace MicroUI;
Event tap(int x,int y) {
  Event event=touch(x,y,true);
  assert(touch(x,y,false).key==nullptr);
  return event;
}
void expect(const Event& event,const char* name) {
  assert(event.key && std::strcmp(event.key,name)==0 && event.action==1);
}
void open() {
  input={};
  assert(tap(888,49).key==nullptr && input.settingsMode);
  assert(tap(640,648).key==nullptr && input.bluetoothPage);
}
int main() {
  open();
  expect(tap(1071,160),"ble_toggle"); // persisted off: initialize on request
  assert(tap(346,553).key==nullptr);  // unavailable: cannot pair
  assert(tap(934,553).key==nullptr && !input.bluetoothForgetConfirm);
  input.bleAvailable=true;
  expect(tap(1071,160),"ble_toggle");
  assert(tap(346,553).key==nullptr);  // disabled: do not start pairing
  input.bleEnabled=true;
  assert(!input.bleReady); // HID-ready requires pairing/connection first
  expect(tap(346,553),"ble_pair");
  input.blePairing=true;
  assert(tap(346,553).key==nullptr);  // no repeated pairing request
  input.blePairing=false;
  expect(touch(1071,160,true),"ble_toggle");
  assert(touch(346,553,true).key==nullptr); // drag does not start pairing
  assert(touch(346,553,false).key==nullptr);

  input.bleBonded=true;
  assert(touch(934,553,true).key==nullptr && input.bluetoothForgetConfirm);
  assert(touch(876,509,true).key==nullptr); // first finger cannot confirm
  assert(input.bluetoothForgetConfirm);
  assert(touch(876,509,false).key==nullptr);
  expect(tap(876,509),"ble_forget");       // separate deliberate second tap
  assert(!input.bluetoothForgetConfirm);
  assert(tap(934,553).key==nullptr && input.bluetoothForgetConfirm);
  assert(tap(404,509).key==nullptr && !input.bluetoothForgetConfirm); // cancel
  assert(tap(934,553).key==nullptr && input.bluetoothForgetConfirm);
  assert(tap(1181,49).key==nullptr && !input.bluetoothForgetConfirm && input.bluetoothPage);
  assert(tap(934,553).key==nullptr && input.bluetoothForgetConfirm);
  input.bleBonded=false;
  assert(tap(876,509).key==nullptr); // bond removed while dialog was open
  assert(!input.bluetoothForgetConfirm);

  assert(tap(1181,49).key==nullptr && !input.bluetoothPage && input.settingsMode);
  assert(tap(700,300).key==nullptr && input.volume==30); // old volume target
  assert(tap(700,500).key==nullptr && input.brightness==40); // old brightness target
  assert(tap(640,648).key==nullptr && input.bluetoothPage);
  assert(tap(888,49).key==nullptr && !input.settingsMode && !input.bluetoothPage);
  assert(MicroControls::nativeSamples==0); // all settings gestures stay local

  State state{};
  assert(std::strcmp(bluetoothStatus(state),"Bluetooth off")==0);
  state.bleEnabled=true;
  assert(std::strcmp(bluetoothStatus(state),"Starting Bluetooth")==0);
  std::strcpy(state.bleError,"Synthetic initialization failure");
  assert(std::strcmp(bluetoothStatus(state),"Bluetooth unavailable")==0);
  state.bleError[0]=0;
  state.bleAvailable=true;
  assert(std::strcmp(bluetoothStatus(state),"Not paired")==0);
  state.bleBonded=true;
  assert(std::strcmp(bluetoothStatus(state),"Reconnecting")==0);
  state.blePairing=true;
  assert(std::strcmp(bluetoothStatus(state),"Waiting for pairing")==0);
  state.bleConnected=true;
  assert(std::strcmp(bluetoothStatus(state),"Waiting for pairing")==0);
  state.blePairing=false;
  assert(std::strcmp(bluetoothStatus(state),"Connecting")==0);
  state.bleReady=true;
  assert(std::strcmp(bluetoothStatus(state),"Bluetooth connected")==0);
  state.activeTransport=Transport::Usb;
  assert(std::strcmp(activeTransportLabel(state),"USB cable")==0);
  assert(std::strcmp(transportReceipt(state,true),"USB submitted")==0);
  state.activeTransport=Transport::Bluetooth;
  assert(std::strcmp(transportReceipt(state,false),"BLE not sent")==0);
  state.activeTransport=Transport::Usb;
  char pin[16];
  state.blePairing=true;
  bluetoothPasskey(state,pin,sizeof(pin));
  assert(std::strcmp(pin,"------")==0); // no generated PIN yet
  state.blePasskeyValid=true;
  state.blePasskey=0;
  bluetoothPasskey(state,pin,sizeof(pin));
  assert(std::strcmp(pin,"000000")==0); // zero is a valid six-digit passkey
  state.blePasskey=123;
  bluetoothPasskey(state,pin,sizeof(pin));
  assert(std::strcmp(pin,"000123")==0);
  state.blePairing=false;
  bluetoothPasskey(state,pin,sizeof(pin));
  assert(std::strcmp(pin,"------")==0); // completed pairing hides stale PIN
  UiLocale::setLanguage(UiLocale::Language::Chinese);
  assert(std::strcmp(activeTransportLabel(state),"USB 有线")==0);
  assert(std::strcmp(bluetoothStatus(state),"蓝牙已连接")==0);
  std::cout << "Bluetooth UI touch and state contracts passed\n";
}
'''
        with tempfile.TemporaryDirectory(prefix='tab5-bluetooth-ui-') as directory:
            cpp = Path(directory) / 'test.cpp'
            binary = Path(directory) / 'test'
            cpp.write_text(harness)
            build = subprocess.run([COMPILER, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                                    '-Iinclude', str(cpp), 'src/ui_locale.cpp', '-o', str(binary)],
                                   cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Bluetooth UI touch and state contracts passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
