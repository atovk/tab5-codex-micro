"""Execute the actual firmware admission/security callbacks with a fake BLE host.

No radio or hardware access. The fake supplies connection facts and NVS timing;
the admission, trust, callback ordering and security decisions are production.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class CallbackOrderTests(unittest.TestCase):
    def test_early_auth_cccd_mtu_and_delayed_connect(self):
        compiler=shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source=(ROOT/"src/micro_ble.cpp").read_text()
        callbacks=source[source.index("PeerIdentity identity("):source.index("class NativeOutput")]
        listener=source[source.index("int gapListener("):source.index("BLECharacteristic* report(")]
        repair=source[source.index("bool allowRepair(uint16_t handle)"):source.index("}  // namespace MicroBle",source.index("bool allowRepair(uint16_t handle)"))]
        forget=source[source.index("void clearBonds()"):source.index("void disconnect()",source.index("void clearBonds()"))]
        with tempfile.TemporaryDirectory(prefix="tab5-ble-order-") as directory:
            root=Path(directory)
            (root/"ble_callbacks.inc").write_text(callbacks+listener+repair+forget)
            binary=root/"callbacks"
            result=subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                "-I",str(ROOT/"include"),"-I",str(root),str(ROOT/"tests/test_ble_callback_order.cpp"),"-o",str(binary)],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=="__main__":
    unittest.main()
