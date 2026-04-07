# Fossil Museum Installation — System Configuration

## Hardware
- **Computer**: Intel NUC i5 (Alder Lake-P, Iris Xe Graphics)
- **Sensor**: Azure Kinect DK (depth + microphone array)
- **LiDAR**: RPLIDAR C1 (footstep detection)
- **Display**: 1920x1080 via HDMI
- **Audio**: HDA Intel PCH analog out (headphone/line-out jack)
- **UPS**: GoldenMate 600VA/360W LiFePO4 (76.8Wh)
- **Power Control**: ezOutlet5 (remote power cycling)
- **USB Hub**: Powered USB hub between NUC and Kinect

## Power Chain
```
Wall → UPS → ezOutlet5 → NUC + Kinect (via powered USB hub)
```

## OS
- Ubuntu 24.04.4 LTS (Noble Numbat)
- Kernel: 6.17.0-20-generic

## Boot Sequence
1. BIOS: After Power Loss → Power On (auto-starts after power outage)
2. GDM auto-login as `martin`
3. Custom X session (`fossil.desktop`) → `start.sh` → `session.sh`
4. `session.sh` sets up X environment (DPMS off, cursor hidden, matchbox WM)
5. `pj-monitor.service` detects projector state, starts/stops `fossil-app.service`
6. `env-check` (1 min cron) monitors Kinect, triggers ezOutlet5 power cycle if missing

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

## App Lifecycle
```
pj-monitor.service (always running)
    │
    ├── Projector ON  → systemctl start fossil-app.service
    ├── Projector OFF → systemctl stop fossil-app.service
    ├── App crash     → restart fossil-app.service
    └── PJ unreachable → no change (app keeps current state)

session.sh (X session)
    └── X environment only: xset, unclutter, matchbox, white bg
        Exports DISPLAY/XAUTHORITY to /tmp/fossil-display-env
```

## Systemd Services
| Service | Purpose | Restart |
|---------|---------|---------|
| `fossil-app.service` | Runs `python main.py` | No (pj-monitor controls) |
| `pj-monitor.service` | Monitors projector, controls app | Always |
| `boot-notify.service` | Pushover on boot | Oneshot |
| `shutdown-notify.service` | Pushover on shutdown | Oneshot |
| `nut-server.service` | UPS data server | Enabled |
| `nut-monitor.service` | UPS event notifications (no shutdown) | Enabled |

## ezOutlet5 Setup
- **Model**: ezOutlet5 (EZ-72)
- **Hostname**: ezoutlet
- **Wired IP**: 10.0.0.2 (static, subnet 255.255.255.0, gateway 10.0.0.1)
- **WiFi**: SSID `ANY`, DHCP
- **HTTP Port**: 80
- **Default credentials**: admin / 1AA51F
- **Cloud**: Disabled

### ezOutlet5 Settings
| Setting | Value |
|---------|-------|
| Outlet Mode | Auto Reset |
| Ping Target | 10.0.0.1 (NUC wired) |
| Ping Interval | 60 sec |
| Ping Fail Delay | 10 min |
| Power Off Duration | 10 sec |
| Max Resets | Unlimited |
| WiFi Signal Check | Disabled (Disregard) |

### ezOutlet5 API
```bash
# Reset (power off, wait 10s, power on)
curl -u admin:1AA51F "http://10.0.0.2/overview?reset=Reset"

# Turn outlet OFF
curl -u admin:1AA51F "http://10.0.0.2/overview?onoff=OFF"

# Turn outlet ON
curl -u admin:1AA51F "http://10.0.0.2/overview?onoff=ON"

# Configuration pages
http://10.0.0.2/overview    # Status + outlet control
http://10.0.0.2/network     # Network settings
http://10.0.0.2/settings    # Auto-reset, ping config
http://10.0.0.2/list        # Ping target address
```

### ezOutlet5 Network Setup (from scratch)
1. Factory default: DHCP on Ethernet, connect to find IP
2. Set static IP: `http://<ip>/network` → Ethernet DHCP Off, IP 10.0.0.2, Mask 255.255.255.0, Gateway 10.0.0.1
3. Set WiFi: Enable, SSID and password for museum network, DHCP On
4. Set hostname: `ezoutlet`
5. Disable cloud
6. Set auto-reset: `http://<ip>/settings` → Auto Reset, ping delay 10 min, power off 10s, unlimited resets, signal check Disregard, ping interval 60s
7. Set ping target: `http://<ip>/list` → 10.0.0.1, PING mode

## UPS
- **Model**: GoldenMate 600VA/360W LiFePO4 (76.8Wh)
- **USB ID**: 075d:0300 (-BMS- Smart-Battery)
- **Interface**: USB HID (monitor-only — no load switching)
- **Runtime**: ~110 minutes on battery for NUC
- **NUT**: Driver + server enabled for monitoring, upsmon for notifications
- **SHUTDOWNCMD**: `/bin/true` (no auto-shutdown — S5 state is fatal)
- **Notifications**: Instant Pushover via `/usr/local/bin/nut-notify.sh`

## Projector Control
- **Protocol**: Google TV Remote (androidtvremote2) on ports 6466/6467
- **Pairing**: One-time PIN exchange, certs stored in `.pj-certs/`
- **Control**: `pj-control.py <name> status|on|off|pair`
- **Config**: `pj-config.json` maps names to IPs
- **Fallback**: ADB on port 5555 (requires Developer Options enabled)

## Private Network (wired)
| Device | Hostname | IP |
|--------|----------|----|
| NUC | fossil | 10.0.0.1 |
| ezOutlet5 | ezoutlet | 10.0.0.2 |
| Fossil projector | fossil-pj | 10.0.0.10 |
| Haste projector | haste-pj | 10.0.0.11 |

## Monitoring & Notifications
| Check | Method | Frequency | Action |
|-------|--------|-----------|--------|
| NUC online | healthchecks.io ping | 5 min | Alert on miss |
| Environment | env-check.sh cron | 1 min | Pushover + healthchecks |
| CPU temp > 85°C | env-check | 1 min | Pushover alert |
| Kinect missing | env-check | 1 min | ezOutlet5 power cycle (max 3/hr) |
| Disk > 90% | env-check | 1 min | Pushover alert |
| WiFi signal < -70dBm | env-check | 1 min | Pushover alert |
| UPS on battery | NUT upsmon | Instant | Pushover alert |
| UPS power restored | NUT upsmon | Instant | Pushover alert |
| Projector state | pj-monitor | 15 sec | Start/stop app |
| System boot | systemd oneshot | On boot | Pushover |
| System shutdown | systemd oneshot | On shutdown | Pushover |

## Key System Files

### /usr/share/xsessions/fossil.desktop
Custom X session — bypasses GNOME, runs fossil kiosk directly.

### /home/martin/fossil/session.sh
X environment setup only: DPMS off, cursor hidden, white background, matchbox WM. Exports display env to `/tmp/fossil-display-env`. No app lifecycle management.

### /home/martin/fossil/start.sh
XAUTHORITY detection wrapper — finds Wayland or X11 auth file dynamically.

### /home/martin/fossil/env-check.sh
Environmental monitoring: temp, Kinect, disk, WiFi. Triggers ezOutlet5 power cycle for Kinect recovery. Pushover notifications with recovery alerts.

### /home/martin/fossil/pj-monitor.py
Projector state monitor. Persistent connection via Google TV Remote protocol. Controls fossil-app.service start/stop. Restarts app on crash.

### /home/martin/fossil/pj-control.py
Standalone projector control CLI: status, on, off, pair.

### /usr/local/bin/nut-notify.sh
UPS event → Pushover notification bridge.

### /etc/gdm3/custom.conf
Auto-login as martin, default session = fossil.desktop.

### /etc/udev/rules.d/
- `99-k4a.rules` — Azure Kinect USB permissions
- `90-kinect-no-v4l2.rules` — Prevent PipeWire from claiming Kinect cameras
- `99-rplidar.rules` — RPLIDAR serial port permissions
- `99-ups.rules` — UPS HID permissions

### /etc/default/grub
```
GRUB_CMDLINE_LINUX_DEFAULT="consoleblank=0 xhci_hcd.quirks=0x80 usbcore.autosuspend=-1 quiet splash"
GRUB_RECORDFAIL_TIMEOUT=0
```

### /etc/logrotate.d/fossil
Daily rotation, 7 days retained, compressed, max 10MB.

## Disabled Services
- cups, bluetooth, ModemManager, snapd
- unattended-upgrades, apt-daily timers
- whoopsie, kerneloops, apport
- power-profiles-daemon, switcheroo-control
- colord, fwupd, packagekit, tracker
- sleep/suspend/hibernate targets masked

## Packages (beyond Ubuntu base)
- libk4a1.4, libk4a1.4-dev (Azure Kinect SDK)
- intel-opencl-icd (Kinect depth engine on Intel GPU)
- libportaudio2, libsndfile1 (audio)
- libgl1-mesa-dev (OpenGL)
- unclutter (cursor hiding)
- matchbox-window-manager (keyboard focus)
- cec-utils, avahi-daemon, avahi-utils
- nut (UPS monitoring)
- tailscale (remote access VPN)

## Python Venv (/home/martin/fossil/.venv)
pyk4a, opencv-python, numpy, moderngl, pygame, Pillow, pyserial,
sounddevice, soundfile, scipy, rplidar-roboticia, androidtvremote2

## Known Issues
- **Kinect depth camera intermittent boot failure**: The depth camera (045e:097c)
  sometimes fails to enumerate on USB at boot (error -71). env-check triggers
  ezOutlet5 hard power cycle to recover. Max 3 attempts per hour.
- **DPMS**: Even with xset -dpms, the "Enabled" flag persists but timeouts are
  set to 0 so no blanking occurs.
- **WiFi power save**: Must be disabled (`wifi.powersave = 2`) or the radio
  sleeps and stops responding to ARP/ping.
- **WiFi 6GHz**: Forced to 5GHz band to avoid driver/router quirks.
- **Soft reboot is harmful**: Never use `sudo reboot` — causes S5 state which
  prevents BIOS auto-power-on. Always use ezOutlet5 hard power cycle.

## Remote Access
```bash
ssh martin@nuc.local        # local network (WiFi)
ssh martin@100.108.88.51    # Tailscale (anywhere)
ssh martin@10.0.0.1         # private wired network
```

## Monitoring Commands
```bash
tail -f /var/log/fossil.log                    # app + pj-monitor output
systemctl status fossil-app.service            # app status
systemctl status pj-monitor.service            # projector monitor
upsc ups                                       # UPS status
cat /sys/class/thermal/thermal_zone*/temp      # temperatures
python3 pj-control.py haste-pj status          # projector state
curl -u admin:1AA51F http://10.0.0.2/overview  # ezOutlet5 status
```
