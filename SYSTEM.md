# Fossil Museum Installation — System Configuration

## Hardware
- **Computer**: Intel NUC i5 (Alder Lake-P, Iris Xe Graphics)
- **Sensor**: Azure Kinect DK (depth + microphone array)
- **LiDAR**: RPLIDAR C1 (footstep detection)
- **Display**: 1920x1080 via HDMI
- **Audio**: HDA Intel PCH analog out (headphone/line-out jack)

## OS
- Ubuntu 24.04.4 LTS (Noble Numbat)
- Kernel: 6.17.0-20-generic

## Boot Sequence
1. BIOS: After Power Loss → Power On (auto-starts after power outage)
2. GDM auto-login as `martin`
3. Custom X session (`fossil.desktop`) — no GNOME desktop
4. `session.sh` disables DPMS/screensaver, hides cursor, runs fossil in restart loop
5. App restarts on crash, scheduled restart every 24h

## BIOS Settings (AMI)
- After Power Loss → **Power On**
- Deep S4/S5 → **Disabled**
- ErP/EuP → **Disabled**
- USB S4/S5 Power → **Enabled**
- Pseudo G3 → **Disabled**
- IGD Minimum Memory → **Maximum**
- IGP Aperture Size → **Maximum**
- Fast Boot → **Enabled**
- Quiet Boot → **Enabled**

## Key System Files

### /usr/share/xsessions/fossil.desktop
Custom X session — bypasses GNOME, runs fossil directly.

### /home/martin/fossil/session.sh
Kiosk session: disables DPMS, hides cursor, white background, runs fossil in a restart loop with 24h scheduled restarts.

### /home/martin/fossil/start.sh
XAUTHORITY detection wrapper — finds Wayland or X11 auth file dynamically.

### /home/martin/fossil/run.sh
Restart wrapper: handles exit codes (0=clean, 75=sensor failure, 124=timeout).

### /etc/systemd/system/fossil.service
Systemd service (currently disabled — session.sh handles restarts instead).

### /etc/gdm3/custom.conf
Auto-login as martin, default session = fossil.desktop.

### /etc/udev/rules.d/
- `99-k4a.rules` — Azure Kinect USB permissions (0666, plugdev)
- `90-kinect-no-v4l2.rules` — Prevent PipeWire from claiming Kinect cameras
- `99-rplidar.rules` — RPLIDAR serial port permissions (0666, plugdev)

### /etc/default/grub
`consoleblank=0` — prevents console from blanking

### /etc/systemd/logind.conf
HandleLidSwitch=ignore, IdleAction=ignore, IdleActionSec=0

### /etc/default/apport
`enabled=0` — crash reporter disabled

### ~/.config/monitors.xml
Forces 1920x1080@60Hz on HDMI-1

## Disabled Services
- cups, bluetooth, ModemManager, snapd
- unattended-upgrades, apt-daily timers
- whoopsie, kerneloops, apport
- power-profiles-daemon, switcheroo-control
- colord, fwupd, packagekit, tracker
- sleep/suspend/hibernate targets masked

## Packages (beyond Ubuntu base)
- libk4a1.4, libk4a1.4-dev (Azure Kinect SDK, from Microsoft repo)
- intel-opencl-icd (required for Kinect depth engine on Intel GPU)
- libportaudio2, libsndfile1 (audio)
- libgl1-mesa-dev (OpenGL)
- unclutter (cursor hiding)
- cec-utils, avahi-daemon, avahi-utils

## Python Venv (/home/martin/fossil/.venv)
pyk4a, opencv-python, numpy, moderngl, pygame, Pillow, pyserial,
sounddevice, soundfile, scipy, rplidar-roboticia

## Known Issues
- **Kinect depth camera intermittent boot failure**: The depth camera (045e:097c)
  sometimes fails to enumerate on USB at boot (error -71). A powered USB hub
  between the NUC and Kinect resolves this. Software USB resets do not help.
- **DPMS**: Even with xset -dpms, the "Enabled" flag persists but timeouts are
  set to 0 so no blanking occurs.

## Network
- Hostname: nuc
- WiFi enabled for remote access
- Avahi/mDNS: advertises as nuc.local

## Remote Access
```
ssh martin@nuc.local   # or by IP
```

## Monitoring
```
tail -f /var/log/fossil.log     # app output
systemctl status fossil.service  # service status (if enabled)
top -bn1 | grep python          # CPU usage
```
