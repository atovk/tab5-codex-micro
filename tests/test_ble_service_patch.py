"""Execute the patched SDK insertion method with the SDK's pointer-keyed map."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ble_service_patch", ROOT / "tools/ble_security_patch.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)
COMPILER = shutil.which("c++")
SDK = ROOT / ".pio-core/packages/framework-arduinoespressif32/libraries/BLE/src/BLEService.cpp"


def original_method():
    if SDK.is_file():
        source = SDK.read_text(encoding="utf-8")
        # Validate the complete pinned source before extracting its real method.
        PATCH.patch_service_source(source)
        start = source.index("void BLEService::addCharacteristic(BLECharacteristic *pCharacteristic) {")
        end = source.index("}  // addCharacteristic", start) + len("}  // addCharacteristic")
        return source[start:end]
    # CI does not need the firmware SDK: preserve the exact old insertion anchor.
    return """void BLEService::addCharacteristic(BLECharacteristic *pCharacteristic) {
  BLECharacteristic *pExisting = m_characteristicMap.getByUUID(pCharacteristic->getUUID());
  if (pExisting != nullptr) { log_w("duplicate UUID"); }
""" + PATCH.SERVICE_CHARACTERISTIC_INSERT + """
#if defined(CONFIG_NIMBLE_ENABLED)
  getServer()->serviceChanged();
#endif
}
"""


PREFIX = r"""
#include <array>
#include <cassert>
#include <cstdint>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <vector>
#define log_v(...) ((void)0)
#define log_d(...) ((void)0)
#define log_w(...) ((void)0)
using String = std::string;
using ble_uuid_t = uint16_t;
struct NativeUUID { ble_uuid_t u; };
struct BLEUUID {
  NativeUUID native;
  explicit BLEUUID(uint16_t value) : native{value} {}
  bool equals(BLEUUID other) const { return native.u == other.native.u; }
  String toString() const { return std::to_string(native.u); }
  const NativeUUID *getNative() const { return &native; }
};
struct BLECharacteristic {
  BLEUUID m_bleUUID{0x2a4d};
  uint16_t m_properties;
  uint16_t m_handle = 0;
  int m_removed = 0;
  std::array<uint8_t, 2> reportReference;
  BLECharacteristic(uint16_t properties, uint8_t id, uint8_t type)
      : m_properties(properties), reportReference{{id, type}} {}
  BLEUUID getUUID() const { return m_bleUUID; }
  static int handleGATTServerEvent() { return 0; }
};
struct BLECharacteristicMap {
  std::map<BLECharacteristic *, String> m_uuidMap;
  BLECharacteristic *getByUUID(BLEUUID uuid) const {
    for (auto &myPair : m_uuidMap) {
      if (myPair.first->getUUID().equals(uuid)) { return myPair.first; }
    }
    return nullptr;
  }
  void setByUUID(BLECharacteristic *pCharacteristic, BLEUUID uuid) {
    m_uuidMap.insert(std::pair<BLECharacteristic *, String>(pCharacteristic, uuid.toString()));
  }
};
struct Server { int changes = 0; void serviceChanged() { ++changes; } };
struct BLEService {
  BLECharacteristicMap m_characteristicMap;
  Server server;
  Server *getServer() { return &server; }
  String toString() const { return "service"; }
  void addCharacteristic(BLECharacteristic *pCharacteristic);
};
struct Definition {
  const ble_uuid_t *uuid = nullptr;
  int (*access_cb)() = nullptr;
  void *arg = nullptr;
  uint16_t flags = 0;
  int min_key_size = -1;
  uint16_t *val_handle = nullptr;
};
std::vector<Definition> definitions(BLEService &service) {
  std::vector<Definition> result(service.m_characteristicMap.m_uuidMap.size());
  Definition *pChr_a = result.data();
  size_t i = 0;
  for (auto &entry : service.m_characteristicMap.m_uuidMap) {
    BLECharacteristic *pCharacteristic = entry.first;
"""

SERIALIZER_END = """    ++i;
  }
  return result;
}
"""

MAIN = r"""
int main() {
  BLEService service;
  std::array<BLECharacteristic, 5> reports{{
      {0x12, 1, 1}, {0x0a, 1, 2}, {0x12, 6, 1}, {0x0a, 6, 2}, {0x0a, 6, 3}}};
  for (auto &report : reports) { service.addCharacteristic(&report); }
  assert(service.m_characteristicMap.m_uuidMap.size() == 5);
  auto defs = definitions(service);
  assert(defs.size() == reports.size());
  std::set<void *> objects;
  std::set<uint16_t *> destinations;
  std::set<std::array<uint8_t, 2>> references;
  uint16_t handle = 40;
  for (auto &def : defs) {
    auto *report = static_cast<BLECharacteristic *>(def.arg);
    assert(service.m_characteristicMap.m_uuidMap.count(report) == 1);
    assert(def.uuid == &report->m_bleUUID.getNative()->u && *def.uuid == 0x2a4d);
    assert(def.access_cb == BLECharacteristic::handleGATTServerEvent);
    assert(def.flags == report->m_properties && def.min_key_size == 0);
    assert(def.val_handle == &report->m_handle);
    assert(objects.insert(def.arg).second);
    assert(destinations.insert(def.val_handle).second);
    assert(references.insert(report->reportReference).second);
    *def.val_handle = handle++;
  }
  assert((references == std::set<std::array<uint8_t, 2>>{
      {{1, 1}}, {{1, 2}}, {{6, 1}}, {{6, 2}}, {{6, 3}}}));
  std::set<uint16_t> handles;
  for (auto &report : reports) { assert(report.m_handle && handles.insert(report.m_handle).second); }
  auto *first = service.m_characteristicMap.getByUUID(BLEUUID(0x2a4d));
  auto *other = &reports[0] == first ? &reports[1] : &reports[0];
  assert(other != first);
  first->m_removed = 2;
  other->m_removed = 1;
  service.addCharacteristic(other);
  assert(service.m_characteristicMap.m_uuidMap.size() == 5);
  assert(first->m_removed == 2);
#if defined(CONFIG_NIMBLE_ENABLED)
  assert(other->m_removed == 0);
  assert(service.server.changes == 6);
#else
  assert(other->m_removed == 1);
  assert(service.server.changes == 0);
#endif
  service.addCharacteristic(first);
  assert(service.m_characteristicMap.m_uuidMap.size() == 5);
#if defined(CONFIG_NIMBLE_ENABLED)
  assert(first->m_removed == 0);
#else
  assert(first->m_removed == 2);
#endif
  std::cout << "five report objects and handles; same-object re-add preserved\n";
}
"""


class ServiceAnchorTests(unittest.TestCase):
    def test_insertion_missing_duplicate_or_changed_fails_closed(self):
        insertion = PATCH.SERVICE_CHARACTERISTIC_INSERT
        for bad in ("", insertion * 2, insertion.replace("pExisting->m_removed = 0;", "pExisting->m_removed = 1;")):
            with self.subTest(anchor=bad):
                with self.assertRaises(RuntimeError):
                    PATCH.patch_service_source(bad + PATCH.SERVICE_OBJECT_DEFINITION)

    def test_object_definition_missing_duplicate_or_changed_fails_closed(self):
        definition = PATCH.SERVICE_OBJECT_DEFINITION
        mutations = ("", definition * 2,
                     definition.replace(".arg = pCharacteristic;", ".arg = nullptr;"),
                     definition.replace(".flags = pCharacteristic->m_properties;", ".flags = 0;"),
                     definition.replace(".val_handle = &pCharacteristic->m_handle;", ".val_handle = nullptr;"))
        for bad in mutations:
            with self.subTest(anchor=bad):
                with self.assertRaises(RuntimeError):
                    PATCH.patch_service_source(PATCH.SERVICE_CHARACTERISTIC_INSERT + bad)


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class ServiceBehaviorTests(unittest.TestCase):
    def compile_and_run(self, method, patched, nimble):
        # The real serializer assignments are compiled too, so each object's
        # callback context, properties and value-handle destination are checked.
        source = PREFIX + PATCH.SERVICE_OBJECT_DEFINITION + SERIALIZER_END + method + MAIN
        if patched:
            source = PATCH.patch_service_source(source)
        with tempfile.TemporaryDirectory(prefix="tab5-ble-service-") as directory:
            path = Path(directory) / "probe.cpp"
            binary = Path(directory) / "probe"
            path.write_text(source, encoding="utf-8")
            backend = "CONFIG_NIMBLE_ENABLED" if nimble else "CONFIG_BLUEDROID_ENABLED"
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-D" + backend, str(path), "-o", str(binary)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

    def test_nimble_registers_each_report_and_restores_exact_object(self):
        self.compile_and_run(original_method(), patched=True, nimble=True)

    def test_bluedroid_behavior_matches_unpatched_sdk(self):
        method = original_method()
        self.assertEqual(self.compile_and_run(method, patched=False, nimble=False),
                         self.compile_and_run(method, patched=True, nimble=False))


if __name__ == "__main__":
    unittest.main()
