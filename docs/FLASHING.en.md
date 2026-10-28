# Flash the downloaded release

[简体中文](FLASHING.md) · **English**

For **Tab5 Codex Micro 0.13.0**, targeting the ESP32-P4 **M5Stack Tab5 with 16MB flash and 32MB PSRAM**. This guide uses a precompiled bundle. You do not need to clone the repository, install PlatformIO, or compile firmware.

This release adds a standard USB microphone alongside USB and Bluetooth controls. Microphone audio is USB-only; see the [microphone guide](MICROPHONE.en.md). **Firmware and fonts are still flashed over USB; wireless flashing is not provided.** After flashing, follow the [Bluetooth guide](BLUETOOTH.en.md) for system PIN pairing and optional title/state companion setup. The firmware uses compatible firmware already on Tab5's onboard C6. Do not flash the C6 separately or flash other connected development boards. Other C6/ESP-Hosted firmware combinations are unverified.

## 1. Download and extract

Download **`tab5-codex-micro-0.13.0.zip`** from the [0.13.0 Release](https://github.com/atovk/tab5-codex-micro/releases/tag/v0.13.0) and extract the entire archive into one directory. Do not run the script from inside the ZIP or download only GitHub's automatically generated “Source code” archive.

| File | Purpose |
| --- | --- |
| `firmware.factory.bin` | Complete factory image with bootloader, partition table, and application; written at `0x0` |
| `title-font.bin` | Complete dynamic title font; written at `0x510000` |
| `flash.py` | Flashing helper independent of the source build environment |
| `requirements-flash.txt` | Python dependencies for flashing |
| `manifest.json`, `SHA256SUMS` | Release file metadata and checksums |
| `FLASHING.md`, `FLASHING.en.md` | Chinese and English flashing guides |
| `BLUETOOTH.md`, `BLUETOOTH.en.md`, `COMPANION.md` | System pairing, automatic reconnection, and optional local synchronization |
| `MICROPHONE.md`, `MICROPHONE.en.md` | Chinese and English USB microphone guides |
| `images/microphone-day.png`, `images/microphone-night.png` | Microphone guide images for offline viewing |
| `assets/tab5-microphone-light-0.13.0.png`, `assets/tab5-microphone-dark-0.13.0.png` | Real Tab5 microphone-page captures in English |

Keep these files in the same directory. Separate `.bin` assets are available for existing workflows, but the complete ZIP is recommended for a first installation so the font and checksums are not omitted.

The default full flash replaces the existing Tab5 firmware and partition layout and resets NVS, including this project's Bluetooth bonds, theme, volume, brightness, and language preferences. If existing bonds become invalid, remove the old computer-side record and pair again. Back up flash beforehand if you want to restore the factory system later. Do not flash this image to ESP32-S3 or other boards.

## 2. Prepare Python

Use Python **3.11+** and a USB-C **data cable**. The flashing helper supports macOS/Linux. Windows supports offline `--check` only; flashing is not supported. USB microphone checks were performed on macOS. Detailed native Bluetooth and companion results retain the 0.12.0 macOS baseline; this release checked enumeration and bond retention without repeating every Bluetooth execution case. These checks do not establish Linux desktop compatibility.

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

Fresh installations default to **English**. Open the gear at the top right and use **Language** to choose English or Chinese. Language, theme, audio, brightness, microphone gain/mute, and saved Bluetooth bonds survive ordinary restarts. Flashing the factory image again resets these preferences and bonds.

Configure the device in **Settings → Codex Micro** in the Codex desktop app. Dial, joystick, and keys use native Micro mappings. USB data has priority. Bluetooth requires system pairing first; follow the [Bluetooth guide](BLUETOOTH.en.md). This firmware keeps battery charging disabled. Bluetooth replaces the data link, so Tab5 still needs a battery or suitable power source.

For USB microphone input, select **Tab5 Microphone** in macOS Sound Input or the recording application, then open **Gear → Microphone** on Tab5 for meters, mute, and gain. It needs no companion or virtual audio driver. Voice keeps its desktop mapping without automatically selecting the input. Bluetooth carries controls and metadata, not microphone audio; see the [microphone guide](MICROPHONE.en.md).

Chat titles and real Fast/Plan/permission metadata require the additional macOS local bridge; they are not supplied by native HID lighting messages. The bundle's flashing dependencies do not include the companion. Use the [Bluetooth guide](BLUETOOTH.en.md#title-and-settings-companion) or this version's [English README](https://github.com/atovk/tab5-codex-micro/blob/v0.13.0/README.en.md#optional-chat-titles-and-settings-bridge) to prepare the source and full dependencies.

A separate read-only **Tracked settings** strip displays the tracked chat's real next-send settings, including Full access. Missing or expired metadata is unknown. Native Fast/Plan/Approve buttons do not represent that chat's settings or establish their action target, so the interface explicitly shows **Target not verified**. Changing the selected desktop chat does not change the bridge target, and the bridge does not redirect native keys to the tracked chat.

**USB submitted / Bluetooth submitted** only means the complete message has been handed to the corresponding transport stack, not that the desktop executed it. **USB not sent / Bluetooth not sent** means submission failed or the queue rejected the event, with no promise of later delivery.

## Later updates

Download and extract the complete ZIP for each new version and follow its instructions. If a compatible full title font is already installed, skip rewriting it:

```sh
.venv/bin/python flash.py --port /dev/cu.usbmodemXXXX --skip-font
```

`--skip-font` **still writes the factory image and overwrites NVS, so it does not preserve Bluetooth bonds or local preferences**. It only skips the font. Do not use it for the first installation. If release notes require a font update, use the default full flash again. Pause tools using the serial port before updating and restart the companion afterward.

The bundle's `flash.py` **does not provide `--app-only`**. To preserve bonds and preferences, use the [v0.13.0 source helper](https://github.com/atovk/tab5-codex-micro/blob/v0.13.0/tools/flash.py). Prepare and build that version using [Build from source](https://github.com/atovk/tab5-codex-micro/blob/v0.13.0/README.en.md#build-from-source), then run:

```sh
# Run in the v0.13.0 source checkout, not the downloaded bundle directory.
.venv/bin/python tools/flash.py --port /dev/cu.usbmodemXXXX --skip-build --app-only
```

This writes only the application at `0x10000`. **It applies only when this project's compatible partition layout and complete, valid title font are already installed and this update requires no migration.** It does not inspect the device's previous partition table for compatibility. Do not use it for first installation, factory restoration, or partition/font upgrades. Under these conditions, the untouched NVS, bonds, preferences, and font can be retained. The 0.12.0 baseline verified reconnection after an application update without a new PIN. The microphone update verified that saved bonds and microphone preferences remained; it did not repeat every BLE execution test. This does not guarantee that every future version can skip a full flash.

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
