"""USB/BLE priority, bounded handoff, independent framing and reply routing."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class MicroConnectionTests(unittest.TestCase):
    def test_connection_policy(self):
        with tempfile.TemporaryDirectory(prefix="tab5-connection-") as directory:
            binary = Path(directory) / "test_micro_connection"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Iinclude",
                 "tests/test_micro_connection.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("14/14 connection tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
