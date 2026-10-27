"""Run the Arduino-free sound cue policy contracts with a host C++ compiler."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class SoundFeedbackTests(unittest.TestCase):
    def test_feedback_policy(self):
        with tempfile.TemporaryDirectory(prefix="tab5-sound-feedback-") as directory:
            binary = Path(directory) / "test_sound_feedback"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_sound_feedback.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("7/7 sound feedback tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
