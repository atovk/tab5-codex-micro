"""Run the Arduino-free host feedback interpretation contracts."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class HostFeedbackTests(unittest.TestCase):
    def test_native_lighting_and_configuration_contract(self):
        with tempfile.TemporaryDirectory(prefix="tab5-host-feedback-") as directory:
            binary = Path(directory) / "test_host_feedback"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_host_feedback.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run(
                [str(binary)], text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("8/8 host feedback tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
