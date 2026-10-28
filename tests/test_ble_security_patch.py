"""Host execution of the SDK patch's repair gate and bounded sync wait."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ble_security_patch", ROOT / "tools" / "ble_security_patch.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)
COMPILER = shutil.which("c++")


class PatchAnchorTests(unittest.TestCase):
    def test_unknown_or_duplicate_server_anchors_fail_closed(self):
        original = PATCH.SERVER_INCLUDE + PATCH.SERVER_GUARD
        for source in ("", original.replace("ble_store_util_delete_peer", "changed_api"), original + PATCH.SERVER_GUARD):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError):
                    PATCH.patch_server_source(source)

    def test_unknown_or_duplicate_wait_anchors_fail_closed(self):
        for source in ("", PATCH.DEVICE_WAIT.replace("delay(1)", "delay(2)"), PATCH.DEVICE_WAIT * 2):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError):
                    PATCH.patch_device_source(source)


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class PatchBehaviorTests(unittest.TestCase):
    def compile_and_run(self, source, directory):
        path = Path(directory) / "probe.cpp"
        binary = Path(directory) / "probe"
        platform_flags = []
        if sys.platform == "darwin":
            # Mach-O spells an optional, undefined reference weak_import;
            # firmware and Linux/GCC use the ELF weak spelling unchanged.
            source = source.replace("__attribute__((weak))", "__attribute__((weak_import))")
            if "tab5_ble_allow_repair" in source:
                platform_flags = ["-Wl,-U,_tab5_ble_allow_repair"]
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
             "-I", str(directory), str(path), "-o", str(binary)] + platform_flags,
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repair_missing_denied_and_allowed_hook(self):
        fixture = """#include <cstdint>
#include <cassert>
#include <services/gatt/ble_svc_gatt.h>
constexpr int BLE_GAP_REPEAT_PAIRING_IGNORE = 0, BLE_GAP_REPEAT_PAIRING_RETRY = 1;
struct Desc { uint16_t conn_handle; int peer_id_addr; };
struct Event { struct { uint16_t conn_handle; } repeat_pairing; };
int deleted = 0, lookupResult = 0;
int ble_gap_conn_find(uint16_t handle, Desc* desc) {
  *desc = {handle, 42}; return lookupResult;
}
void ble_store_util_delete_peer(const int* peer) { assert(*peer == 42); ++deleted; }
int repair(Event* event) {
  int rc; Desc desc;
""" + PATCH.SERVER_GUARD + """  return BLE_GAP_REPEAT_PAIRING_RETRY;
}
"""
        patched = PATCH.patch_server_source(fixture)
        with tempfile.TemporaryDirectory(prefix="tab5-ble-gate-") as directory:
            header = Path(directory) / "services/gatt/ble_svc_gatt.h"
            header.parent.mkdir(parents=True)
            header.write_text("", encoding="utf-8")
            # A missing application hook must preserve the bond.
            self.compile_and_run(patched + """
int main() { Event e{{7}}; assert(repair(&e) == 0); assert(deleted == 0); }
""", directory)
            # The hook is consulted only after resolving the current connection.
            self.compile_and_run(patched + """
bool permit = false; int calls = 0;
extern "C" bool tab5_ble_allow_repair(uint16_t handle) {
  assert(handle == 7); ++calls; return permit;
}
int main() {
  Event e{{7}};
  assert(repair(&e) == 0); assert(deleted == 0); assert(calls == 1);
  permit = true;
  assert(repair(&e) == 1); assert(deleted == 1); assert(calls == 2);
  lookupResult = 1;
  assert(repair(&e) == 0); assert(deleted == 1); assert(calls == 2);
}
""", directory)

    def test_sync_success_timeout_and_clock_wraparound(self):
        fixture = """#include <cstdint>
#include <cassert>
bool m_synced = false;
uint32_t clockMs = 0, delays = 0, syncAfter = 0, errors = 0;
uint32_t millis() { return clockMs; }
void ble_npl_time_delay(int ticks) {
  assert(ticks == 1); ++clockMs; ++delays;
  if (syncAfter && delays == syncAfter) m_synced = true;
}
void log_e(const char*) { ++errors; }
bool initialize() {
""" + PATCH.DEVICE_WAIT + """  return true;
}
int main() {
  syncAfter = 12; assert(initialize()); assert(delays == 12); assert(errors == 0);
  m_synced = false; delays = 0; syncAfter = 0;
  assert(!initialize()); assert(delays == 10000); assert(errors == 1);
  clockMs = UINT32_MAX - 50; delays = 0;
  assert(!initialize()); assert(delays == 10000); assert(errors == 2);
  m_synced = true; delays = 0;
  assert(initialize()); assert(delays == 0);
}
"""
        with tempfile.TemporaryDirectory(prefix="tab5-ble-sync-") as directory:
            self.compile_and_run(PATCH.patch_device_source(fixture), directory)


if __name__ == "__main__":
    unittest.main()
