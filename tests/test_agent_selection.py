"""A dynamic Agent press clears the unrelated explicit chat until reboot."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class AgentSelectionTests(unittest.TestCase):
    def test_dynamic_slot_does_not_reuse_fixed_chat(self):
        with tempfile.TemporaryDirectory(prefix="tab5-agent-selection-") as directory:
            binary = Path(directory) / "test_agent_selection"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Iinclude",
                 "tests/test_agent_selection.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("6/6 Agent selection tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
