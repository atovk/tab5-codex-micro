"""Scheduling, wraparound and failed-NVS retry contracts without hardware."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class RenderServiceTests(unittest.TestCase):
    def test_scheduling_and_retry(self):
        with tempfile.TemporaryDirectory(prefix="tab5-render-") as directory:
            binary = Path(directory) / "test_render_service"
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_render_service.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shadow_occlusion_preserves_every_pixel(self):
        with tempfile.TemporaryDirectory(prefix="tab5-material-") as directory:
            binary = Path(directory) / "test_material_occlusion"
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Itests/stubs/material", "-Iinclude", "tests/test_material_occlusion.cpp",
                 "-o", str(binary)], cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("51 material occlusion pixel comparisons passed", result.stdout)
