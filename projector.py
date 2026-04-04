#!/usr/bin/env python3
"""Control Optoma projector via RS232 through AV Access 4KEX70-L."""

import sys
import glob
import time
import subprocess

FTDI_VID = "0403"  # FTDI FT232R USB UART
BAUD = 9600


def find_rs232_port():
    """Find RS232 serial port by FTDI vendor ID."""
    for dev in sorted(glob.glob("/dev/ttyUSB*")):
        try:
            result = subprocess.run(
                ["udevadm", "info", "--query=property", f"--name={dev}"],
                capture_output=True, text=True
            )
            if f"ID_VENDOR_ID={FTDI_VID}" in result.stdout:
                return dev
        except Exception:
            continue
    return None


def send_command(port, cmd):
    """Send RS232 command to projector and read response."""
    import serial
    with serial.Serial(port, BAUD, timeout=2) as ser:
        ser.write(cmd.encode())
        time.sleep(0.5)
        response = ser.read(ser.in_waiting or 1)
        return response.decode(errors='replace').strip()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print("Usage: projector.py [on|off|status]")
        sys.exit(1)

    action = sys.argv[1]
    port = find_rs232_port()
    if not port:
        print("Error: RS232 adapter not found (looking for FTDI VID 0403)")
        sys.exit(1)

    print(f"Using {port}")

    if action == "on":
        resp = send_command(port, "~0000 1\r")
        print(f"Power ON sent. Response: {resp}")
    elif action == "off":
        resp = send_command(port, "~0000 0\r")
        print(f"Power OFF sent. Response: {resp}")
    elif action == "status":
        resp = send_command(port, "~00124 1\r")
        print(f"Status query sent. Response: {resp}")


if __name__ == "__main__":
    main()
