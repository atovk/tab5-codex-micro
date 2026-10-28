"""Run the Arduino-free touch contract tests when a host C++ compiler exists."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class MicroControlsTests(unittest.TestCase):
    def test_touch_contract(self):
        with tempfile.TemporaryDirectory(prefix="tab5-controls-") as directory:
            binary = Path(directory) / "test_micro_controls"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Iinclude", "src/micro_controls.cpp",
                 "tests/test_micro_controls.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("21/21 touch tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
