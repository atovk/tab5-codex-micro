"""Delayed BLE diagnostics: authenticated pre-ARM, bounded readiness, and cleanup."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class MicroBleDiagnosticsTests(unittest.TestCase):
    def test_diagnostic_schedule(self):
        with tempfile.TemporaryDirectory(prefix="tab5-ble-diagnostics-") as directory:
            binary = Path(directory) / "test_micro_ble_diagnostics"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Iinclude",
                 "tests/test_micro_ble_diagnostics.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("30/30 BLE diagnostic schedule tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
