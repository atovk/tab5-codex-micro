"""Run UTF-8 title wrapping contracts without Arduino, assets or USB."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class TitleLayoutTests(unittest.TestCase):
    def test_utf8_word_wrapping_and_content_preservation(self):
        with tempfile.TemporaryDirectory(prefix="tab5-title-layout-") as directory:
            binary = Path(directory) / "test_title_layout"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_title_layout.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run(
                [str(binary)], text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("10/10 title layout tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
