"""Run sensor/display-independent orientation contracts with a host compiler."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class OrientationPolicyTests(unittest.TestCase):
    def test_orientation_policy(self):
        with tempfile.TemporaryDirectory(prefix="tab5-orientation-policy-") as directory:
            binary = Path(directory) / "test_orientation_policy"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_orientation_policy.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("15/15 orientation policy tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
