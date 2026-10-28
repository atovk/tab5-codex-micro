"""Exercise transport scheduling with failed USB submissions and disconnects."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class MicroTransportTests(unittest.TestCase):
    def test_transport_policy(self):
        with tempfile.TemporaryDirectory(prefix="tab5-transport-") as directory:
            binary = Path(directory) / "test_micro_transport"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Iinclude",
                 "tests/test_micro_transport.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("20/20 transport tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
