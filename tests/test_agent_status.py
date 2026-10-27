"""Run the Arduino-free native status interpretation contract tests."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class AgentStatusTests(unittest.TestCase):
    def test_native_status_contract(self):
        with tempfile.TemporaryDirectory(prefix="tab5-agent-status-") as directory:
            binary = Path(directory) / "test_agent_status"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_agent_status.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("6/6 agent status tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
