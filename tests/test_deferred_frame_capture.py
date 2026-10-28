"""Bounded deferred real-frame ownership plus USB/render integration boundaries."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


class DeferredFrameCaptureTests(unittest.TestCase):
    @unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
    def test_owner_and_schedule(self):
        with tempfile.TemporaryDirectory(prefix="tab5-deferred-frames-") as directory:
            binary = Path(directory) / "test_deferred_frame_capture"
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Iinclude",
                 "tests/test_deferred_frame_capture.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("deferred framebuffer cases passed", result.stdout)

    def test_usb_only_dispatch(self):
        main = (ROOT / "src/main.cpp").read_text()
        dispatch = main.split("void debugCommand()", 1)[1].split("void setup()", 1)[0]
        for command in ("FRAME_CAPTURE ARM ", "FRAME_CAPTURE READ ",
                        "FRAME_CAPTURE STATUS", "FRAME_CAPTURE CLEAR"):
            self.assertIn(command, dispatch)
        ble = (ROOT / "src/micro_ble.cpp").read_text()
        self.assertNotIn("FRAME_CAPTURE", ble)
        self.assertIn('!strncmp(accumulating,"TITLES ",7)', ble)
        self.assertIn('!strncmp(accumulating,"HOST_CONFIG ",12)', ble)

    def test_rendered_evidence_and_no_input_suppression(self):
        main = (ROOT / "src/main.cpp").read_text()
        ui = (ROOT / "src/micro_ui.cpp").read_text()
        service = main.split("void serviceFrameCapture(", 1)[1].split("void writeCapturedLight", 1)[0]
        self.assertIn("const auto& state=MicroUI::renderedState()", service)
        self.assertNotIn("MicroLink::state()", service)
        self.assertIn("beforeRender.generation==context.generation", service)
        self.assertIn("beforeRender.session==context.session", service)
        self.assertIn("evidence,frameCaptureHooks", service)
        self.assertIn("const State& renderedState() { return previousState; }", ui)
        loop = main.split("void loop()", 1)[1]
        self.assertLess(loop.index("MicroUI::render("), loop.index("serviceFrameCapture(captureBefore)"))
        ble_context = main.split("MicroBleDiagnostics::Context bleTestContext(", 1)[1].split("void cleanupBleTest", 1)[0]
        self.assertNotIn("frameCapture", ble_context)
        self.assertNotIn("captureInputs(", service)
        self.assertNotIn("setDiagnosticFrameActive(", service)
        self.assertNotIn("sendInput(", service)
        checkpoint = main.split("DeferredFrameCapture::Context serviceFrameCaptureInputs()", 1)[1].split("void releaseFrameCapture", 1)[0]
        self.assertIn("serviceInputs()", checkpoint)
        for forbidden in ("render(", "debugCommand(", "applyOrientation(", "RenderService::checkpoint"):
            self.assertNotIn(forbidden, checkpoint)
        capture_context = main.split("DeferredFrameCapture::Context frameCaptureContext()", 1)[1].split("void* allocateFrameCapture", 1)[0]
        for gate in ("inputTest", "screenCopy", "capturingInputs()", "M5.Touch.getCount()",
                     "MicroUI::playing()", "MicroUI::settingsOpen()", "lightingPreview", "effectsAreFrozen()"):
            self.assertIn(gate, capture_context)

    def test_private_evidence_and_stream_ownership(self):
        main = (ROOT / "src/main.cpp").read_text()
        status = main.split("void frameCaptureStatus()", 1)[1].split("MicroBleDiagnostics::Context bleTestContext", 1)[0]
        for private in ("title", "identity", "thread_id", "passkey", "Pin", "PIN"):
            self.assertNotIn(private, status)
        read = main.split('command.startsWith("FRAME_CAPTURE READ ")', 1)[1].split('command.startsWith("FRAME_CAPTURE ")', 1)[0]
        self.assertIn("!bleTest.active() && MicroLink::bleDiagnosticIdle()", read)
        self.assertIn("screenCopy=frameCapture.take(index[0])", read)
        self.assertIn("MicroLink::setDiagnosticFrameActive(true)", read)
        self.assertIn("RGB565_BE", read)
        self.assertIn("free(screenCopy); screenCopy=nullptr", main)


if __name__ == "__main__":
    unittest.main()
