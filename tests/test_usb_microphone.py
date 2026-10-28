"""Compile the production USB module against the installed TinyUSB SDK headers.

The mocked boundary is the USB controller/RTOS, not the descriptors or callbacks.
No board, sound-device changes, or firmware build is involved.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")
SDK = ROOT / ".pio-core/packages/framework-arduinoespressif32-libs/esp32p4_es/include/arduino_tinyusb/tinyusb/src"

CONFIG = """
#pragma once
#define CFG_TUSB_MCU OPT_MCU_NONE
#define CFG_TUSB_OS OPT_OS_CUSTOM
#define CFG_TUSB_RHPORT0_MODE (OPT_MODE_DEVICE | OPT_MODE_FULL_SPEED)
#define CFG_TUD_ENDPOINT0_SIZE 64
#define CFG_TUD_AUDIO 1
#define CFG_TUD_AUDIO_ENABLE_EP_IN 1
#define CFG_TUD_AUDIO_FUNC_1_EP_IN_SZ_MAX 200
#define CFG_TUD_AUDIO_FUNC_1_EP_IN_SW_BUF_SZ 6400
#define CFG_TUD_AUDIO_FUNC_1_DESC_LEN 200
"""
USB = """
#pragma once
#include <cstdint>
using esp_event_base_t = const char*;
inline constexpr esp_event_base_t ARDUINO_USB_EVENTS="usb";
enum {ARDUINO_USB_STOPPED_EVENT=1,ARDUINO_USB_SUSPEND_EVENT,ARDUINO_USB_RESUME_EVENT};
using UsbEvent = void(*)(void*,esp_event_base_t,int32_t,void*);
inline UsbEvent eventHandler=nullptr;
struct UsbMock { void onEvent(UsbEvent callback) {eventHandler=callback;} };
inline UsbMock USB;
"""
HAL = """
#pragma once
#include <cstdint>
constexpr int ESP_OK=0, USB_INTERFACE_AUDIO=3;
using Loader = uint16_t(*)(uint8_t*,uint8_t*);
inline Loader descriptorLoader=nullptr;
inline uint16_t registeredLength=0;
inline int tinyusb_enable_interface(int,uint16_t length,Loader loader) {
  registeredLength=length; descriptorLoader=loader; return ESP_OK;
}
inline uint8_t tinyusb_get_free_in_endpoint() {return 3;}
inline uint8_t tinyusb_add_string_descriptor(const char*) {return 6;}
"""
RTOS = """
#pragma once
#include <cstdint>
using TickType_t=uint32_t;
using TaskHandle_t=void*;
constexpr int pdPASS=1,pdTRUE=1,pdFALSE=0;
constexpr TickType_t portMAX_DELAY=UINT32_MAX;
#define pdMS_TO_TICKS(ms) (ms)
inline TickType_t fakeTick=0;
inline thread_local int runIterations=0;
inline void (*onTaskDelay)()=nullptr;
struct EndIteration {};
inline int xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,TaskHandle_t*) {return pdPASS;}
inline TickType_t xTaskGetTickCount() {return fakeTick;}
inline void vTaskDelay(TickType_t ticks) {
  fakeTick+=ticks;
  if (onTaskDelay) onTaskDelay();
  if (--runIterations<=0) throw EndIteration{};
}
"""
SEMAPHORE = """
#pragma once
#include "FreeRTOS.h"
#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>
#include <cassert>
struct TestSemaphore {
  std::timed_mutex mutex;
  std::atomic<unsigned> blockingTakes{0},tryTakes{0};
  std::atomic<size_t> owner{0};
};
using SemaphoreHandle_t=TestSemaphore*;
inline size_t semaphoreThread() {return std::hash<std::thread::id>{}(std::this_thread::get_id());}
inline SemaphoreHandle_t xSemaphoreCreateMutex() {return new TestSemaphore;}
inline void vSemaphoreDelete(SemaphoreHandle_t semaphore) {delete semaphore;}
inline int xSemaphoreTake(SemaphoreHandle_t semaphore,TickType_t timeout) {
  if (!semaphore) return pdFALSE;
  bool acquired=false;
  if (!timeout) {++semaphore->tryTakes; acquired=semaphore->mutex.try_lock();}
  else {
    ++semaphore->blockingTakes;
    if (timeout==portMAX_DELAY) {semaphore->mutex.lock(); acquired=true;}
    else acquired=semaphore->mutex.try_lock_for(std::chrono::milliseconds(timeout));
  }
  if (acquired) semaphore->owner.store(semaphoreThread());
  return acquired ? pdTRUE : pdFALSE;
}
inline int xSemaphoreGive(SemaphoreHandle_t semaphore) {
  assert(semaphore->owner.load()==semaphoreThread());
  semaphore->owner.store(0); semaphore->mutex.unlock(); return pdTRUE;
}
"""
CUSTOM_OS = """
#pragma once
// Only the RTOS boundary is mocked. SDK tu_fifo_t and its public DMA API retain
// their real declarations, including separate write/read mutex fields.
typedef SemaphoreHandle_t osal_mutex_t;
typedef unsigned char osal_mutex_def_t;
typedef void* osal_task_handle_t;
typedef void* osal_queue_t;
"""


@unittest.skipUnless(COMPILER and SDK.is_dir(), "host C++ compiler and local TinyUSB SDK required")
class UsbMicrophoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="tab5-uac-")
        cls.addClassCleanup(cls.directory.cleanup)
        folder = Path(cls.directory.name)
        (folder / "freertos").mkdir()
        for name, source in {
            "tusb_config.h": CONFIG, "USB.h": USB,
            "esp32-hal-tinyusb.h": HAL,
            "freertos/FreeRTOS.h": RTOS,
            "freertos/task.h": '#pragma once\n#include "FreeRTOS.h"\n',
            "freertos/semphr.h": SEMAPHORE,
            "tusb_os_custom.h": CUSTOM_OS,
        }.items():
            (folder / name).write_text(source)
        cls.binary = folder / "usb_microphone"
        result = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror",
             "-Wno-missing-field-initializers", "-Wno-gnu-zero-variadic-macro-arguments",
             "-pthread", "-fsanitize=undefined", "-fno-sanitize-recover=all",
             "-I" + str(folder), "-Iinclude", "-Isrc", "-isystem", str(SDK),
             "tests/test_usb_microphone.cpp", "-o", str(cls.binary)],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def check(self, case):
        result = subprocess.run([str(self.binary), case], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("USB microphone contract passed", result.stdout)

    def test_actual_sdk_uac1_composite_descriptor(self):
        self.check("descriptor")

    def test_host_alt_setting_controls_and_local_mute(self):
        self.check("controls")

    def test_capture_epoch_disconnect_and_reconnect_discard_old_pcm(self):
        self.check("epochs")

    def test_mute_silence_volume_and_real_usb_tx_counters(self):
        self.check("pcm")

    def test_bounded_spsc_full_wrap_and_concurrent_integrity(self):
        self.check("queue")

    def test_control_close_reopen_fences_a_paused_fifo_publish(self):
        self.check("publish-reopen")

    def test_local_mute_fences_a_paused_fifo_publish(self):
        self.check("publish-mute")

    def test_fifo_dma_copy_handles_a_wrapped_write(self):
        self.check("fifo-wrap")

    def test_fifo_full_and_contended_mutex_drop_without_waiting(self):
        self.check("fifo-full")
