# Flash the downloaded release

[简体中文](FLASHING.md) · **English**

For **Tab5 Codex Micro 0.11.1**, targeting the ESP32-P4 **M5Stack Tab5 with 16MB flash and 32MB PSRAM**. This guide uses a precompiled bundle. You do not need to clone the repository, install PlatformIO, or compile firmware.

This release bundle supports USB only. USB/BLE support is part of unreleased 0.12.0 source development, with hardware validation ongoing; see the [Bluetooth guide](BLUETOOTH.en.md). Bluetooth does not provide wireless flashing. Firmware and fonts are still written over USB.

## 1. Download and extract

Download **`tab5-codex-micro-0.11.1.zip`** from the [0.11.1 Release](https://github.com/atovk/tab5-codex-micro/releases/tag/v0.11.1) and extract the entire archive into one directory. Do not run the script from inside the ZIP or download only GitHub's automatically generated “Source code” archive.

| File | Purpose |
| --- | --- |
| `firmware.factory.bin` | Complete factory image with bootloader, partition table, and application; written at `0x0` |
| `title-font.bin` | Complete dynamic title font; written at `0x510000` |
| `flash.py` | Flashing helper independent of the source build environment |
| `requirements-flash.txt` | Python dependencies for flashing |
| `manifest.json`, `SHA256SUMS` | Release file metadata and checksums |
| `FLASHING.md`, `FLASHING.en.md` | Chinese and English flashing guides |

Keep these files in the same directory. Separate `.bin` assets are available for existing workflows, but the complete ZIP is recommended for a first installation so the font and checksums are not omitted.

Flashing replaces the existing Tab5 firmware and partition layout and resets this project's theme, volume, brightness, and language preferences. Back up flash beforehand if you want to restore the factory system later. Do not flash this image to ESP32-S3 or other boards.

## 2. Prepare Python

Use Python **3.11+** and a USB-C **data cable**. The commands below target macOS/Linux; the Windows workflow has not been verified.

Open a terminal in the extracted directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-flash.txt
.venv/bin/python flash.py --check
```

`--check` validates local release files without flashing a device. It checks file SHA-256 hashes, the P4 image layout, the complete firmware version, and the font format. The firmware version must exactly match the release manifest; an RC or longer version string cannot stand in for the stable release. If validation fails, download and extract the complete ZIP again rather than bypassing the error.

You can also verify `SHA256SUMS` with system tools:

```sh
# macOS
shasum -a 256 -c SHA256SUMS

# Linux
sha256sum -c SHA256SUMS
```

## 3. Connect and identify Tab5

Connect the cable to the **USB-C port on the Tab5 body** and keep it powered. Close serial monitors and screenshot tools using that port. If the title companion is running, stop the foreground process or pause the login service using `tools/title_sync_service.py stop` from its source checkout. The flashing helper also uses a compatible serial lock to coordinate access.

```sh
.venv/bin/python flash.py --list-ports
```

Identify the Tab5 port. macOS commonly uses `/dev/cu.usbmodemXXXX`; Linux commonly uses `/dev/ttyACM0`. With multiple Espressif devices connected, select the P4 Tab5 explicitly rather than relying on the USB vendor ID alone.

- Already running this project's firmware: select its `Tab5 Codex Micro` application port. The helper requests download mode.
- First installation or no usable application port: hold Tab5 **Reset for about two seconds** until the internal green LED flashes rapidly, release it, run `--list-ports` again, and select the new P4 download port.

## 4. Flash firmware and font

Replace the example port with the actual port you identified:

```sh
# macOS example
.venv/bin/python flash.py --port /dev/cu.usbmodemXXXX

# Linux example
.venv/bin/python flash.py --port /dev/ttyACM0
```

The helper validates the release files before using the ESP32-P4 downloader to write:

| Address | Content |
| --- | --- |
| `0x0` | `firmware.factory.bin` |
| `0x510000` | `title-font.bin` |

Keep the cable connected and power stable until the helper finishes. It handles the P4 download/reset state; no manual register commands are required. After a success message and restart, Tab5 should display the console.

## 5. First start

Fresh installations default to **English**. Open the gear at the top right and use **Language** to choose English or Chinese. Language, theme, audio, and brightness survive ordinary restarts. Flashing the factory image again resets these preferences.

Configure the device in **Settings → Codex Micro** in the Codex desktop app. Dial, joystick, and keys use native Micro mappings. Chat titles and real Fast/Plan/permission metadata require the additional macOS local bridge. Follow this version's [English README](https://github.com/atovk/tab5-codex-micro/blob/v0.11.1/README.en.md) or [中文 README](https://github.com/atovk/tab5-codex-micro/blob/v0.11.1/README.md).

Version 0.11.1 displays the tracked chat's real next-send settings, including Full access, in a separate read-only **Tracked settings** strip. Missing or expired metadata is unknown. Native Fast/Plan/Approve buttons do not represent that chat's settings or establish their action target, so the interface explicitly shows **Target not verified**. Changing the selected desktop chat does not change the bridge target, and the bridge does not redirect native keys to the tracked chat.

**USB submitted** only means the complete message has been handed to the USB stack, not that the desktop executed it. **USB not sent** means submission failed or the queue rejected the event, with no promise of later delivery.

## Later updates

Download and extract the complete ZIP for each new version and follow its instructions. If a compatible full title font is already installed, skip rewriting it:

```sh
.venv/bin/python flash.py --port /dev/cu.usbmodemXXXX --skip-font
```

`--skip-font` only skips the font; it does not preserve local settings in NVS. Do not use it for the first installation. If release notes require a font update, use the default full flash again. Pause tools using the serial port before updating and restart the companion afterward.

## Troubleshooting

| Situation | Action |
| --- | --- |
| Tab5 is not listed | Check the data cable and body USB-C connection; reconnect or enter download mode and list ports again |
| Multiple devices found | Use `--port` for the confirmed P4 Tab5; do not try unknown ports or disturb other boards |
| Serial port busy | Close serial monitors and screenshot scripts, pause the companion, and retry |
| Linux permission denied | Ensure your user can access that serial device, then reconnect |
| Missing files or checksum failure | Download the complete release ZIP again and extract all files together |
| Downloader identifies ESP32-S3 | Stop and identify the P4 Tab5 port again |
| Broken fonts or titles | Include `title-font.bin` on first installation; run a full flash without `--skip-font` |
| Titles or states are unknown | Check local bridge configuration and freshness using the README; repeated flashing is unnecessary |
| Device remains in download mode | Keep the cable connected and briefly press Reset. If the application still does not appear, re-enter download mode and run the helper again |

## License

Original code uses the MIT License. Font data uses SIL Open Font License 1.1; other components retain their licenses. The release bundle includes the relevant license files. See the [project repository](https://github.com/atovk/tab5-codex-micro) for source and third-party notices.
