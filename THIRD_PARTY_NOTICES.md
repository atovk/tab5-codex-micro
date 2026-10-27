# Third-party notices

Original project code is licensed under the [MIT License](LICENSE). The following
components retain their own copyright notices and licenses.

## Tab5 UI Sans / Tab5 Title Sans font data

`include/ui_font.h` and the generated `artifacts/tab5-title-font-v1.bin` contain
converted bitmap glyphs from Noto Sans SC Medium, release Sans2.004. These font
derivatives are licensed under the **SIL Open Font License 1.1**, independently
of the project's MIT code license. The generated faces use the project names
Tab5 UI Sans and Tab5 Title Sans.

- Copyright 2014–2021 Adobe (http://www.adobe.com/).
- Source: https://github.com/notofonts/noto-cjk/tree/Sans2.004
- License: [licenses/Tab5-UI-Sans-OFL.txt](licenses/Tab5-UI-Sans-OFL.txt)
- The generator pins the upstream font SHA-256 before conversion.

## TinyUSB HID class

`tools/vendor/hid_device.c` is from TinyUSB 0.21.0, copyright (c) 2019 Ha Thach
(tinyusb.org), under the **MIT License**. The complete upstream copyright and
license remain at the top of the source file. `tools/usb_fs_patch.py` validates
its SHA-256 and applies the project-specific patch to a build-directory copy.

- Source: https://github.com/hathach/tinyusb/blob/0.21.0/src/class/hid/hid_device.c

## Build dependencies

M5Unified, M5GFX, ArduinoJson, Arduino-ESP32, ESP-IDF, pioarduino, and the Python
tooling are fetched separately by the build/install tools. They are not
relicensed by this repository. Refer to each dependency's upstream license and
the notices included in its installed package when redistributing binaries.

Codex, OpenAI, M5Stack, and related product names belong to their respective
owners. This community project is not an official OpenAI or M5Stack release.
