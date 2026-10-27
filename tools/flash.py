#!/usr/bin/env python3
"""Build and flash Tab5, including its USB-C software download-mode recovery."""
import argparse
from pathlib import Path
import subprocess
import sys
import time
import serial
from serial.tools import list_ports
from tab5_transport import serial_lock
from build_title_font import inspect_asset

ROOT = Path(__file__).resolve().parents[1]
VID, APP_PID, ROM_PID = 0x303A, 0x8360, 0x1001

def matching(pid):
    return [p.device for p in list_ports.comports() if p.vid == VID and p.pid == pid]

def run(args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='Initial Tab5 serial port (auto-detected by default)')
    parser.add_argument('--skip-build',action='store_true',help='Flash the already-built factory image')
    parser.add_argument('--font-image',type=Path,
                        help='Also write a validated T5VLW4 title font to the reserved data partition')
    args=parser.parse_args()
    pio=ROOT/'.venv/bin/pio'
    if not args.skip_build:
        run([sys.executable,ROOT/'tools/prepare_platform.py'])
        run([pio,'run'])
    image=ROOT/'.pio/build/tab5/firmware.factory.bin'
    if not image.is_file(): raise RuntimeError('Build the Tab5 firmware before flashing')
    if image.stat().st_size > 0x510000:
        raise RuntimeError('Factory image overlaps the title-font data partition')
    if args.font_image:
        args.font_image=args.font_image.expanduser().resolve()
        metadata=inspect_asset(args.font_image.read_bytes())
        print(f"Validated title font: {metadata['glyph_count']} glyphs, {metadata['file_size']} bytes",flush=True)
    with serial_lock(10):
        flash(args,image)

def flash(args,image):
    apps=matching(APP_PID)
    if apps and (args.port is None or args.port in apps):
        port=args.port or (apps[0] if len(apps)==1 else None)
        if not port: raise RuntimeError('Several Micro devices; specify --port')
        existing_roms=set(matching(ROM_PID))
        print('Requesting Tab5 ROM download mode...', flush=True)
        connection=serial.Serial(port=None,baudrate=115200,timeout=1)
        connection.dtr=True; connection.rts=False; connection.port=port
        connection.open(); connection.write(b'BOOTLOADER\n'); connection.flush()
        time.sleep(.3); connection.close()
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            new_roms=set(matching(ROM_PID))-existing_roms
            if len(new_roms)==1:
                args.port=new_roms.pop()
                break
            time.sleep(.3)
        else:
            raise RuntimeError('The Tab5 ROM port did not appear after BOOTLOADER')
    roms=matching(ROM_PID)
    port=args.port or (roms[0] if len(roms)==1 else None)
    if not port: raise RuntimeError('Expected one Tab5 ROM serial port; specify --port')
    # P4 software entry leaves a sticky force-download bit. Clear only that bit
    # and then reset; this is also harmless for a first flash from ROM/JTAG.
    python=Path(sys.executable)
    # Keep the ROM downloader active through the uploader's final reset.
    run([python,'-m','esptool','--chip','esp32p4','--port',port,'--before','default-reset',
         '--after','no-reset','write-mem','0x50110008','0x4','0x4'])
    write=[python,'-m','esptool','--chip','esp32p4','--port',port,'--baud','1500000',
           '--before','no-reset','--after','no-reset','write-flash','0',image]
    if args.font_image:
        write += ['0x510000',args.font_image]
    run(write)
    run([python,'-m','esptool','--chip','esp32p4','--port',port,'--before','no-reset',
         '--after','watchdog-reset','write-mem','0x50110008','0x0','0x4'])
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        apps=matching(APP_PID)
        if apps:
            print('Tab5 Codex Micro connected: '+', '.join(apps),flush=True)
            return
        time.sleep(.5)
    raise RuntimeError('Flash succeeded, but Micro USB enumeration was not observed')

if __name__=='__main__':
    main()
