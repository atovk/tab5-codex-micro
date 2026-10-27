"""Run packed-font validation and virtual VLW decoding without Arduino or USB."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class TitleFontAssetTests(unittest.TestCase):
    def test_validation_metrics_and_virtual_reads(self):
        with tempfile.TemporaryDirectory(prefix="tab5-title-font-") as directory:
            binary = Path(directory) / "test_title_font_asset"
            build = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_title_font_asset.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run(
                [str(binary)], text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("10/10 title font asset tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
