#pragma once

#include <Arduino.h>
#include <esp_system.h>
#include <sdkconfig.h>

#if !CONFIG_IDF_TARGET_ESP32P4
#error "Tab5 ROM downloader helper requires ESP32-P4"
#endif

#include <esp_private/periph_ctrl.h>
#include <hal/usb_serial_jtag_ll.h>
#include <soc/lp_system_reg.h>
#include <soc/soc.h>
#include <tusb.h>

// Arduino-ESP32 3.3.12's usb_persist_restart() does not implement ESP32-P4.
// On P4, LP_SYS SYS_CTRL bit 2 forces the ROM downloader after CPU restart.
// The bit is sticky: after flashing, clear it before requesting a normal boot:
//   esptool --chip esp32p4 --port PORT --before no-reset --after hard-reset \
//       write-mem 0x50110008 0x0 0x4
// Use --after no-reset on the preceding write-flash invocation. P4 esptool's
// hard_reset() currently does not clear the bit automatically, unlike S3.
//
// Call from the normal Arduino loop after flushing the command acknowledgement.
// This switches USB-C from TinyUSB back to the ROM's USB Serial/JTAG transport.
// The host should wait for a new Espressif USB serial device before flashing.
[[noreturn]] inline void tab5EnterRomBootloader() {
    tud_disconnect();
    delay(120);  // Let the host observe the old composite USB device disconnect.

    REG_SET_BIT(LP_SYSTEM_REG_SYS_CTRL_REG, LP_SYSTEM_REG_FORCE_DOWNLOAD_BOOT);

    PERIPH_RCC_ATOMIC() {
        usb_serial_jtag_ll_enable_bus_clock(true);
    }
    // Tab5 USB-C is FSLS PHY0 (GPIO24/25). The application assigned that PHY
    // to USB_WRAP/OTG; the ROM downloader requires it assigned to USJ instead.
    usb_serial_jtag_ll_phy_select(0);
    usb_serial_jtag_ll_phy_set_defaults();
    usb_serial_jtag_ll_phy_disable_pull_override();

    esp_restart();
    __builtin_unreachable();
}

// Primary references (ESP-IDF v5.5.4 / Espressif esptool):
// components/soc/esp32p4/register/hw_ver1/soc/lp_system_reg.h
// components/hal/esp32p4/include/hal/usb_serial_jtag_ll.h
// components/esp_driver_usb_serial_jtag/src/usb_serial_jtag.c
// esptool/targets/esp32p4.py: RTC_CNTL_OPTION1_REG / FORCE_DOWNLOAD_BOOT_MASK
