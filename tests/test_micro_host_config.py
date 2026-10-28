"""Compile the firmware's real HOST_CONFIG parser with host ArduinoJson.

Only the pure parser functions are selected from the translation unit, keeping
the test independent of ESP32/USB drivers without duplicating schema logic.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")
ARDUINO_JSON = ROOT / ".pio/libdeps/tab5/ArduinoJson/src"


@unittest.skipUnless(COMPILER and (ARDUINO_JSON / "ArduinoJson.h").exists(),
                     "host compiler and project ArduinoJson dependency are required")
class MicroHostConfigTests(unittest.TestCase):
    def test_optional_joystick_mapping_schema(self):
        firmware = (ROOT / "src/micro_link.cpp").read_text()
        start = firmware.index("template <typename T,size_t N>\nbool readEnum(")
        end = firmware.index("bool readChat(", start)
        parser = firmware[start:end]
        harness = r'''
#include <ArduinoJson.h>
#include "host_feedback.h"
#include <assert.h>
#include <iostream>
'''+parser+r'''
JsonDocument snapshot() {
  JsonDocument doc;
  auto error=deserializeJson(doc,R"({"v":1,"seq":1,"source":"local-config",
    "tier":"unknown","encoder":"unknown","voice":"unknown",
    "actions":["unknown","unknown","unknown","unknown","unknown","unknown","unknown"]})");
  assert(!error); return doc;
}
int main() {
  unsigned tests=0;
  {
    auto doc=snapshot(); HostFeedback::Config result; result.joystickUpPlan=true;
    bool available=false;
    assert(readConfig(doc,result,available) && available && !result.joystickUpPlan); ++tests;
  }
  for (const char* mapping:{"unknown","plan"}) {
    auto doc=snapshot(); doc["joystick_up"]=mapping;
    HostFeedback::Config result; bool available=false;
    assert(readConfig(doc,result,available) && available);
    assert(result.joystickUpPlan==!strcmp(mapping,"plan")); ++tests;
  }
  for (const char* wire:{"null","true","1","[]","{}","\"PLAN\"","\"custom\"","\"plan\\u0000extra\""}) {
    auto doc=snapshot(); JsonDocument value; assert(!deserializeJson(value,wire));
    doc["joystick_up"].set(value.as<JsonVariant>());
    HostFeedback::Config result; bool available=false;
    assert(!readConfig(doc,result,available)); ++tests;
  }
  for (const char* mapping:{"unknown","plan"}) {
    auto doc=snapshot(); doc["source"]="unavailable"; doc["joystick_up"]=mapping;
    HostFeedback::Config result; bool available=true;
    assert(readConfig(doc,result,available)==(strcmp(mapping,"plan")!=0));
    assert(!available); ++tests;
  }
  {
    auto doc=snapshot(); doc["source"]="unavailable";
    HostFeedback::Config result; result.joystickUpPlan=true; bool available=true;
    assert(readConfig(doc,result,available) && !available && !result.joystickUpPlan); ++tests;
  }
  std::cout<<tests<<"/14 HOST_CONFIG schema tests passed\n";
  assert(tests==14);
}
'''
        with tempfile.TemporaryDirectory(prefix="tab5-config-schema-") as directory:
            source = Path(directory) / "test.cpp"
            binary = Path(directory) / "test"
            source.write_text(harness)
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                 "-Wno-deprecated-declarations", "-Iinclude", "-I"+str(ARDUINO_JSON),
                 str(source), "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stdout+build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn("14/14 HOST_CONFIG schema tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
