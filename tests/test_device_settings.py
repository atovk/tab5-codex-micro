"""Exercise production preference migration and deferred persistence off-device."""

from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")

ARDUINO = r'''
#pragma once
#include <cstdint>
extern uint32_t mockMillis;
inline uint32_t millis() { return mockMillis; }
template <typename T> T constrain(T value, int low, int high) {
  return value < static_cast<T>(low) ? static_cast<T>(low)
       : value > static_cast<T>(high) ? static_cast<T>(high) : value;
}
'''

PREFERENCES = r'''
#pragma once
#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>
namespace MockNvs {
inline std::map<std::string,uint32_t> values;
inline std::vector<std::string> writes;
inline std::string failKey;
inline bool ready=true;
}
class Preferences {
 public:
  bool begin(const char*,bool) { return MockNvs::ready; }
  uint32_t getUInt(const char* key,uint32_t fallback) {
    auto value=MockNvs::values.find(key);
    return value==MockNvs::values.end() ? fallback : value->second;
  }
  size_t putUInt(const char* key,uint32_t value) {
    MockNvs::writes.emplace_back(key);
    if (MockNvs::failKey==key) {
      MockNvs::failKey.clear();
      return 0;
    }
    MockNvs::values[key]=value;
    return sizeof(value);
  }
};
'''

HARNESS = r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include "Preferences.h"
#include "device_settings.h"
uint32_t mockMillis=0;
void load(const char* file) {
  std::ifstream input(file);
  std::string key;
  uint32_t value;
  while (input>>key>>value) MockNvs::values[key]=value;
}
void store(const char* file) {
  std::ofstream output(file);
  for (const auto& item:MockNvs::values) output<<item.first<<" "<<item.second<<"\n";
}
void output(const DeviceSettings::Values& value) {
  std::cout<<"{\"sound\":"<<int(value.sound)
           <<",\"volume\":"<<int(value.volume)
           <<",\"brightness\":"<<int(value.brightness)
           <<",\"english\":"<<int(value.english)
           <<",\"muted\":"<<int(value.micMuted)
           <<",\"gain\":"<<value.micGain<<"}\n";
}
int main(int argc,char** argv) {
  assert(argc>=3);
  const std::string scenario=argv[1];
  load(argv[2]);
  MockNvs::ready=scenario!="storage-unavailable";
  const auto initial=DeviceSettings::begin();
  assert(!DeviceSettings::pending());
  if (scenario=="read") {
    output(initial);
    assert(MockNvs::writes.empty());
    return 0;
  }
  auto next=initial;
  next.micMuted=true;
  next.micGain=155;
  mockMillis=100;
  if (scenario=="save") {
    assert(argc==9);
    next.sound=std::atoi(argv[3]);
    next.volume=static_cast<uint8_t>(std::atoi(argv[4]));
    next.brightness=static_cast<uint8_t>(std::atoi(argv[5]));
    next.english=std::atoi(argv[6]);
    next.micMuted=std::atoi(argv[7]);
    next.micGain=static_cast<uint16_t>(std::atoi(argv[8]));
  }
  DeviceSettings::update(next);
  assert(DeviceSettings::pending());
  mockMillis=500;
  DeviceSettings::update(next); // Repeating an unchanged UI snapshot must not delay a save.
  mockMillis=699;
  DeviceSettings::poll(false);
  assert(MockNvs::writes.empty());
  if (scenario=="touch") {
    mockMillis=2000;
    DeviceSettings::poll(true);
    assert(MockNvs::writes.empty());
    mockMillis=3000;
    DeviceSettings::poll(false);
    assert(MockNvs::writes.empty() && DeviceSettings::pending());
    mockMillis=3599;
    DeviceSettings::poll(false);
    assert(MockNvs::writes.empty());
    mockMillis=3600;
  } else {
    mockMillis=700;
  }
  if (scenario=="retry-prefs") MockNvs::failKey="prefs";
  if (scenario=="retry-mic") MockNvs::failKey="mic";
  DeviceSettings::poll(false);
  if (scenario=="storage-unavailable") {
    assert(MockNvs::writes.empty() && DeviceSettings::pending());
    mockMillis=10000;
    DeviceSettings::poll(false);
    assert(MockNvs::writes.empty() && DeviceSettings::pending());
    return 0;
  }
  if (scenario=="retry-prefs" || scenario=="retry-mic") {
    const size_t attempted=scenario=="retry-prefs" ? 1 : 2;
    assert(MockNvs::writes.size()==attempted && DeviceSettings::pending());
    mockMillis=1299;
    DeviceSettings::poll(false);
    assert(MockNvs::writes.size()==attempted && DeviceSettings::pending());
    mockMillis=1300;
    DeviceSettings::poll(false);
    assert(MockNvs::writes.size()==attempted+2);
  }
  assert(!DeviceSettings::pending());
  const size_t count=MockNvs::writes.size();
  DeviceSettings::update(next);
  assert(!DeviceSettings::pending());
  mockMillis+=10000;
  DeviceSettings::poll(false);
  assert(MockNvs::writes.size()==count); // A clean state never keeps writing flash.
  store(argv[2]);
  return 0;
}
'''


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class DeviceSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_directory = tempfile.TemporaryDirectory(prefix="tab5-settings-build-")
        cls.addClassCleanup(cls.build_directory.cleanup)
        directory = Path(cls.build_directory.name)
        (directory / "Arduino.h").write_text(ARDUINO)
        (directory / "Preferences.h").write_text(PREFERENCES)
        (directory / "settings.cpp").write_text(HARNESS)
        cls.binary = directory / "settings"
        result = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
             "-I" + str(directory), "-Iinclude", str(directory / "settings.cpp"),
             "src/device_settings.cpp", "-o", str(cls.binary)],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tab5-settings-nvs-")
        self.addCleanup(self.directory.cleanup)
        self.nvs = Path(self.directory.name) / "nvs.txt"

    def seed(self, **values):
        self.nvs.write_text("".join(f"{key} {value}\n" for key, value in values.items()))

    def run_boot(self, scenario="read", *values):
        # Every invocation starts a fresh process, just as a device reboot
        # reinitializes production namespace statics while retaining NVS.
        result = subprocess.run(
            [str(self.binary), scenario, str(self.nvs), *map(str, values)],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def test_fresh_install_uses_defaults_without_writing_flash(self):
        self.assertEqual(self.run_boot(), dict(sound=1, volume=45, brightness=24,
                                               english=1, muted=0, gain=100))
        self.assertFalse(self.nvs.exists())

    def test_old_audio_preferences_load_without_mic_migration_writes(self):
        # Existing installations used the low 16 bits and had no mic key.
        self.seed(prefs=0xA5000000 | 31 << 1 | 60 << 8)
        before = self.nvs.read_bytes()
        self.assertEqual(self.run_boot(), dict(sound=0, volume=31, brightness=60,
                                               english=0, muted=0, gain=100))
        self.assertEqual(self.nvs.read_bytes(), before)

    def test_mute_and_gain_survive_reboot_alongside_other_preferences(self):
        self.run_boot("save", 0, 81, 36, 0, 1, 175)
        self.assertEqual(self.run_boot(), dict(sound=0, volume=81, brightness=36,
                                               english=0, muted=1, gain=175))
        self.run_boot("save", 1, 28, 90, 1, 0, 100)
        self.assertEqual(self.run_boot(), dict(sound=1, volume=28, brightness=90,
                                               english=1, muted=0, gain=100))

    def test_invalid_mic_values_leave_valid_existing_preferences_untouched(self):
        for invalid in (0, 0xA5000001, 0xA6000000 | 201 << 1,
                        0xA6000000 | 255 << 1, 0xA6000201):
            with self.subTest(mic=hex(invalid)):
                self.seed(prefs=0xA5008001 | 67 << 1 | 42 << 8, mic=invalid)
                before = self.nvs.read_bytes()
                self.assertEqual(self.run_boot(), dict(sound=1, volume=67, brightness=42,
                                                       english=1, muted=0, gain=100))
                self.assertEqual(self.nvs.read_bytes(), before)

    def test_gain_range_is_clamped_before_persistence(self):
        for requested, expected in ((0, 0), (200, 200), (201, 200), (65535, 200)):
            with self.subTest(gain=requested):
                self.seed()
                self.run_boot("save", 1, 45, 24, 1, 1, requested)
                self.assertEqual(self.run_boot()["gain"], expected)

    def test_touch_release_restarts_save_delay(self):
        self.run_boot("touch")
        value = self.run_boot()
        self.assertEqual((value["muted"], value["gain"]), (1, 155))

    def test_failed_pref_or_mic_write_stays_pending_and_retries_after_delay(self):
        for failed in ("retry-prefs", "retry-mic"):
            with self.subTest(failed=failed):
                self.seed()
                self.run_boot(failed)
                value = self.run_boot()
                self.assertEqual((value["muted"], value["gain"]), (1, 155))

    def test_storage_unavailable_never_reports_settings_saved(self):
        self.run_boot("storage-unavailable")
        self.assertFalse(self.nvs.exists())


if __name__ == "__main__":
    unittest.main()
