"""Build Arduino's USB glue for the Tab5 USB-C full-speed port.

PlatformIO: extra_scripts = pre:tools/usb_fs_patch.py

Arduino-ESP32 3.3.12 selects the P4 high-speed controller (Tab5 USB-A).
The Tab5 USB-C connector is on FSLS PHY0, GPIO24/25. Compile project-local
copies of the core USB glue with explicit full-speed/rhport-0 initialization,
the PHY0 routing used by M5Stack's own keyboard demo, and 64-byte CDC/HID
endpoint descriptors. Compile the matching upstream TinyUSB HID class with a
64-byte receive buffer, so its interrupt OUT transfer completes after one
full-speed report rather than waiting for eight packets. Add an optional raw
report hook before Arduino's descriptor-based routing. Framework files and
global sdkconfig headers stay untouched.

Sources:
https://github.com/espressif/arduino-esp32/blob/3.3.12/cores/esp32/esp32-hal-tinyusb.c
https://github.com/m5stack/M5Tab5-Keyboard-UserDemo/blob/main/main/usb_init.cpp
https://github.com/hathach/tinyusb/blob/0.21.0/src/class/hid/hid_device.c
"""

import hashlib
from pathlib import Path
import re


HID_SOURCE_SHA256 = "a168e91cac81135905a52eb7da2a2b113134afb5b32ac04307de486146696132"


def patch_usb_source(source):
    """Patch only known device-side anchors, failing on an incompatible core."""
    replacements = [
        (
            "#elif CONFIG_IDF_TARGET_ESP32P4\n#endif",
            '#elif CONFIG_IDF_TARGET_ESP32P4\n'
            '#include "hal/usb_wrap_ll.h"\n'
            '#include "hal/usb_serial_jtag_ll.h"\n'
            '#include "soc/usb_wrap_struct.h"\n#endif',
        ),
        (
            "#if CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31\n"
            "    .otg_speed = USB_PHY_SPEED_HIGH,",
            "#if CONFIG_IDF_TARGET_ESP32S31\n"
            "    .otg_speed = USB_PHY_SPEED_HIGH,",
        ),
        (
            "  ret = usb_new_phy(&phy_config, &phy_handle);\n",
            "  ret = usb_new_phy(&phy_config, &phy_handle);\n"
            "#if CONFIG_IDF_TARGET_ESP32P4\n"
            "  if (ret == ESP_OK) {\n"
            "    // Tab5 USB-C uses FSLS PHY0 (GPIO24/25), not PHY1.\n"
            "    usb_wrap_ll_phy_select(&USB_WRAP, 0);\n"
            "    // The PHY swap also moves unused USB Serial/JTAG onto GPIO26/27.\n"
            "    // Release its pads so the Tab5 speaker keeps DATA/BCLK.\n"
            "    usb_serial_jtag_ll_phy_enable_pad(false);\n"
            "  }\n"
            "#endif\n",
        ),
        (
            "#if CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31\n"
            "  tinit.speed = TUSB_SPEED_HIGH;",
            "#if CONFIG_IDF_TARGET_ESP32S31\n"
            "  tinit.speed = TUSB_SPEED_HIGH;",
        ),
        (
            "  if (!tusb_init(1, &tinit)) { /* TinyUSB: P4 OTG HS is rhport 1 */",
            "  if (!tusb_init(0, &tinit)) { /* Tab5 USB-C: OTG FS is rhport 0 */",
        ),
    ]
    for original, replacement in replacements:
        count = source.count(original)
        if count != 1:
            raise RuntimeError(
                "Tab5 USB-C patch requires Arduino-ESP32 3.3.12; "
                "expected one source anchor, found %d: %r" % (count, original)
            )
        source = source.replace(original, replacement, 1)
    return source


def patch_endpoint_descriptors(source, name):
    """Give full-speed descriptors legal packet sizes without changing ABI.

    The bundled tusb_config.h unconditionally defines CFG_TUD_ENDPOINT_SIZE
    from TUD_OPT_HIGH_SPEED, so a command-line -D cannot safely override it.
    The on-wire descriptors must advertise at most 64 bytes for full-speed
    interrupt and bulk transfers. The HID class buffer is patched separately.
    """
    expected_counts = {"USBHID.cpp": 1, "USBCDC.cpp": 4}
    count = source.count("CFG_TUD_ENDPOINT_SIZE")
    if count != expected_counts[name]:
        raise RuntimeError(
            "Unexpected Arduino-ESP32 endpoint descriptor source in %s: %d anchors"
            % (name, count)
        )
    return source.replace("CFG_TUD_ENDPOINT_SIZE", "64 /* Tab5 USB-C FS packet size */")


def patch_hid_dispatch(source):
    """Observe every HID OUT/control report before Arduino's report-ID lookup."""
    anchor = (
        "void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, "
        "hid_report_type_t report_type, uint8_t const *buffer, uint16_t bufsize) {\n"
    )
    if source.count(anchor) != 1:
        raise RuntimeError("Unexpected Arduino-ESP32 HID report callback source")
    replacement = (
        'extern "C" bool tab5_hid_raw_report(uint8_t instance, uint8_t report_id, '
        "uint8_t report_type, const uint8_t *buffer, uint16_t bufsize) __attribute__((weak));\n\n"
        + anchor
        + "  // A true result means the project handled an otherwise unroutable report.\n"
        + "  if (tab5_hid_raw_report && tab5_hid_raw_report(instance, report_id, "
        "static_cast<uint8_t>(report_type), buffer, bufsize)) {\n"
        "    return;\n"
        "  }\n"
    )
    return source.replace(anchor, replacement, 1)


def patch_hid_class(source):
    """Override the renamed buffer macro before TinyUSB's compatibility alias."""
    anchor = '#include "hid_device.h"\n'
    if source.count(anchor) != 1:
        raise RuntimeError("Unexpected TinyUSB 0.21.0 HID class source")
    source = source.replace(
        anchor,
        anchor
        + "\n#if TUSB_VERSION_NUMBER != 2100\n"
        '#error "Tab5 HID class requires TinyUSB 0.21.0 headers"\n'
        "#endif\n"
        "#if CFG_TUD_HID_EP_BUFSIZE != 64\n"
        '#error "Tab5 full-speed HID receive buffer must be 64 bytes"\n'
        "#endif\n",
        1,
    )
    return "// Project-local Tab5 full-speed HID class.\n#define CFG_TUD_HID_EP_BUFSIZE 64\n" + source


def write_if_changed(destination, content):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_text(encoding="utf-8") != content:
        destination.write_text(content, encoding="utf-8")


def exclude_stock_audio(build_env, node):
    """Only the project microphone owns TinyUSB's global audio callbacks.

    This platform links bundled library objects directly, including unused
    USBAudioCard.cpp. Its strong callbacks conflict with the microphone-only
    UAC1 implementation. Skip that exact stock source without editing the SDK
    or suppressing duplicate-symbol errors for any other component.
    """
    framework = Path(build_env.PioPlatform().get_package_dir("framework-arduinoespressif32"))
    stock = framework / "libraries" / "USB" / "src" / "USBAudioCard.cpp"
    return None if Path(node.srcnode().get_abspath()).resolve() == stock.resolve() else node


def register_platformio(environment):
    if environment.BoardConfig().get("build.mcu") != "esp32p4":
        raise RuntimeError("The Tab5 USB-C patch is only valid for ESP32-P4")

    # Arduino USB declares depends=FS, but PlatformIO does not propagate that
    # bundled library's include path to USBHostMSC/USBMSCFS compilation.
    framework = Path(environment.PioPlatform().get_package_dir("framework-arduinoespressif32"))
    fs_include = framework / "libraries" / "FS" / "src"
    if not (fs_include / "FS.h").is_file():
        raise RuntimeError("Arduino FS headers are missing")
    environment.Append(CPPPATH=[str(fs_include)])

    # Keep the replacement class at the exact version of the installed SDK.
    framework_libs = Path(environment.PioPlatform().get_package_dir("framework-arduinoespressif32-libs"))
    chip_variant = environment.BoardConfig().get("build.chip_variant")
    tinyusb = framework_libs / chip_variant / "include" / "arduino_tinyusb" / "tinyusb" / "src"
    options = (tinyusb / "tusb_option.h").read_text(encoding="utf-8")
    for component, expected in (("MAJOR", 0), ("MINOR", 21), ("REVISION", 0)):
        match = re.search(r"^#define\s+TUSB_VERSION_" + component + r"\s+(\d+)\s*$", options, re.MULTILINE)
        if not match or int(match.group(1)) != expected:
            raise RuntimeError("Tab5 HID replacement requires TinyUSB 0.21.0 SDK headers")
    vendor = Path(environment.subst("$PROJECT_DIR")) / "tools" / "vendor" / "hid_device.c"
    vendor_bytes = vendor.read_bytes()
    if hashlib.sha256(vendor_bytes).hexdigest() != HID_SOURCE_SHA256:
        raise RuntimeError("Vendored TinyUSB 0.21.0 HID class has an unexpected SHA-256")
    hid_class = patch_hid_class(vendor_bytes.decode("utf-8"))
    hid_class_registered = False

    def use_project_usb_glue(build_env, node):
        nonlocal hid_class_registered
        original_path = Path(node.srcnode().get_abspath())
        name = original_path.name
        destination = (
            Path(build_env.subst("$BUILD_DIR"))
            / "tab5-usbc-core"
            / name
        )
        source = original_path.read_text(encoding="utf-8")
        patched = (
            patch_usb_source(source)
            if name == "esp32-hal-tinyusb.c"
            else patch_endpoint_descriptors(source, name)
        )
        if name == "USBHID.cpp":
            patched = patch_hid_dispatch(patched)
            if not hid_class_registered:
                # This middleware runs after Arduino has supplied its include
                # paths and flags, but before the main program is constructed.
                # A direct object precedes SDK archives, replacing hid_device.o.
                hid_class_registered = True
                generated = Path(build_env.subst("$BUILD_DIR")) / "tab5-tinyusb-hid-src"
                write_if_changed(generated / "hid_device.c", hid_class)
                hid_env = build_env.Clone()
                hid_env.Append(CPPPATH=[str(tinyusb), str(tinyusb / "class" / "hid")])
                hid_env.BuildSources(
                    str(Path(build_env.subst("$BUILD_DIR")) / "tab5-tinyusb-hid"),
                    str(generated),
                    "+<hid_device.c>",
                )
                print("Tab5 USB-C: compiling TinyUSB 0.21.0 HID class with 64-byte OUT buffer")
        write_if_changed(destination, patched)
        print("Tab5 USB-C: compiling full-speed %s" % name)
        return build_env.File(str(destination))

    for name in ("esp32-hal-tinyusb.c", "USBHID.cpp", "USBCDC.cpp"):
        environment.AddBuildMiddleware(use_project_usb_glue, "*" + name)
    environment.AddBuildMiddleware(exclude_stock_audio, "*USBAudioCard.cpp")


try:
    Import("env")
except NameError:
    # Allows local validation/import without a SCons environment.
    pass
else:
    register_platformio(env)
