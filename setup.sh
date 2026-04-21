#!/bin/bash
# Fossil Installation - System Setup Script
# Target: Ubuntu 24.04 LTS on Intel NUC
# Run as: sudo ./setup.sh (from the fossil project directory)
set -e

FOSSIL_USER="martin"
FOSSIL_DIR="/home/${FOSSIL_USER}/fossil"

echo "=== Fossil Installation Setup ==="
echo "Target directory: ${FOSSIL_DIR}"

# --- 1. System packages ---
echo ""
echo "--- Installing system packages ---"
apt-get update
apt-get install -y \
    python3-dev python3-venv python3-pip \
    avahi-daemon avahi-utils \
    cec-utils \
    pipewire pipewire-alsa pipewire-audio pipewire-pulse \
    alsa-utils \
    libusb-1.0-0-dev \
    libudev-dev \
    libportaudio2 \
    libgl1-mesa-dev \
    libsndfile1 \
    intel-opencl-icd \
    unclutter \
    matchbox-window-manager \
    curl \
    git

# --- 2. Microsoft repo (for Azure Kinect SDK) ---
echo ""
echo "--- Setting up Azure Kinect SDK ---"
if [ ! -f /usr/share/keyrings/microsoft-archive-keyring.gpg ]; then
    curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-archive-keyring.gpg
fi
if [ ! -f /etc/apt/sources.list.d/microsoft-prod.list ]; then
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-archive-keyring.gpg] https://packages.microsoft.com/ubuntu/18.04/prod bionic main" > /etc/apt/sources.list.d/microsoft-prod.list
    apt-get update
fi
apt-get install -y libk4a1.4 libk4a1.4-dev

# --- 3. Kinect udev rules ---
echo ""
echo "--- Setting up Kinect udev rules ---"
cat > /etc/udev/rules.d/99-k4a.rules << 'EOF'
# Azure Kinect DK udev rules
SUBSYSTEM!="usb", ACTION!="add", GOTO="k4a_logic_rules_end"

ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097a", MODE="0666", GROUP="plugdev"
ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097b", MODE="0666", GROUP="plugdev"
ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097c", MODE="0666", GROUP="plugdev"
ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097d", MODE="0666", GROUP="plugdev"
ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097e", MODE="0666", GROUP="plugdev"

LABEL="k4a_logic_rules_end"
EOF

cat > /etc/udev/rules.d/90-kinect-no-v4l2.rules << 'EOF'
# Prevent pipewire/v4l2 from claiming Azure Kinect cameras
SUBSYSTEM=="video4linux", ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097c", ENV{LIBPIPEWIRE_DONT_MANAGE}="1", TAG-="seat"
SUBSYSTEM=="video4linux", ATTRS{idVendor}=="045e", ATTRS{idProduct}=="097d", ENV{LIBPIPEWIRE_DONT_MANAGE}="1", TAG-="seat"
EOF

cat > /etc/udev/rules.d/99-rplidar.rules << 'EOF'
# RPLIDAR C1 (Silicon Labs CP2102N)
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666", GROUP="plugdev"
EOF

cat > /etc/udev/rules.d/99-ups.rules << 'EOF'
# BMS Smart-Battery UPS (for NUT monitoring)
SUBSYSTEM=="usb", ATTR{idVendor}=="075d", ATTR{idProduct}=="0300", MODE="0666", GROUP="nut"
EOF

udevadm control --reload-rules

# --- 4. Disable WiFi power save (causes unreachable radio) ---
echo ""
echo "--- Disabling WiFi power save ---"
cat > /etc/NetworkManager/conf.d/default-wifi-powersave-on.conf << 'EOF'
[connection]
wifi.powersave = 2
EOF

# --- 5. Add user to plugdev and dialout groups ---
usermod -aG plugdev,dialout ${FOSSIL_USER}

# --- 6. GDM auto-login ---
echo ""
echo "--- Configuring auto-login ---"
cat > /etc/gdm3/custom.conf << EOF
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=${FOSSIL_USER}
DefaultSession=fossil.desktop

[security]

[xdmcp]

[chooser]

[debug]
EOF

# Install fossil X session
cat > /usr/share/xsessions/fossil.desktop << 'EOF'
[Desktop Entry]
Name=Fossil
Comment=Fossil kiosk session
Exec=/home/martin/fossil/start.sh
Type=Application
DesktopNames=Fossil
EOF

# --- 7. Python virtual environment ---
echo ""
echo "--- Setting up Python virtual environment ---"
if [ ! -d "${FOSSIL_DIR}/.venv" ]; then
    sudo -u ${FOSSIL_USER} python3 -m venv "${FOSSIL_DIR}/.venv"
fi
sudo -u ${FOSSIL_USER} "${FOSSIL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u ${FOSSIL_USER} "${FOSSIL_DIR}/.venv/bin/pip" install \
    pyk4a>=1.4.1 \
    opencv-python>=4.8 \
    numpy>=1.24 \
    moderngl>=5.8 \
    pygame>=2.5 \
    Pillow>=10.0 \
    pyserial \
    sounddevice \
    soundfile \
    scipy \
    rplidar-roboticia \
    androidtvremote2 \
    bleak \
    python-kasa

# --- 8. Systemd services ---
echo ""
echo "--- Installing systemd services ---"

# fossil-app.service — runs the app only, controlled by pj-monitor
cat > /etc/systemd/system/fossil-app.service << EOF
[Unit]
Description=Fossil Interactive Installation App
After=graphical.target
Wants=graphical.target

[Service]
Type=simple
User=${FOSSIL_USER}
Group=${FOSSIL_USER}
WorkingDirectory=${FOSSIL_DIR}
EnvironmentFile=/tmp/fossil-display-env
ExecStart=${FOSSIL_DIR}/.venv/bin/python ${FOSSIL_DIR}/main.py
StandardOutput=append:/var/log/fossil.log
StandardError=append:/var/log/fossil.log

ProtectSystem=false
ReadWritePaths=${FOSSIL_DIR} /tmp /var/log /dev

[Install]
WantedBy=graphical.target
EOF

# pj-monitor.service — controls fossil-app based on projector state
cat > /etc/systemd/system/pj-monitor.service << EOF
[Unit]
Description=Projector monitor — controls fossil-app based on projector state
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${FOSSIL_USER}
Group=${FOSSIL_USER}
WorkingDirectory=${FOSSIL_DIR}
Environment=PATH=${FOSSIL_DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${FOSSIL_DIR}/.venv/bin/python ${FOSSIL_DIR}/pj-monitor.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/fossil.log
StandardError=append:/var/log/fossil.log

[Install]
WantedBy=multi-user.target
EOF

# button-monitor.service — Shelly BLU button dispatcher (BLE)
cat > /etc/systemd/system/button-monitor.service << EOF
[Unit]
Description=Shelly BLU button dispatcher
After=bluetooth.service network-online.target
Wants=bluetooth.service network-online.target

[Service]
Type=simple
User=${FOSSIL_USER}
Group=${FOSSIL_USER}
WorkingDirectory=${FOSSIL_DIR}
ExecStart=${FOSSIL_DIR}/.venv/bin/python ${FOSSIL_DIR}/button-monitor.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/fossil.log
StandardError=append:/var/log/fossil.log

[Install]
WantedBy=multi-user.target
EOF

touch /var/log/fossil.log
chown ${FOSSIL_USER}:${FOSSIL_USER} /var/log/fossil.log

# Re-enable bluetooth for BLE scanning (disabled earlier for cleanliness)
systemctl unmask bluetooth.service 2>/dev/null || true
systemctl enable bluetooth.service

systemctl daemon-reload
systemctl disable fossil.service 2>/dev/null || true
systemctl enable pj-monitor.service
systemctl enable button-monitor.service

# --- 9. Audio: set analog output to max ---
echo ""
echo "--- Configuring audio ---"
# Find the PCH card number (may vary by hardware)
PCH_CARD=$(cat /proc/asound/cards | grep PCH | awk '{print $1}')
if [ -n "$PCH_CARD" ]; then
    amixer -c ${PCH_CARD} sset Master 100% on 2>/dev/null || true
    amixer -c ${PCH_CARD} sset Headphone 100% on 2>/dev/null || true
    amixer -c ${PCH_CARD} sset PCM 100% 2>/dev/null || true
    amixer -c ${PCH_CARD} sset 'Line Out' 100% on 2>/dev/null || true
    amixer -c ${PCH_CARD} sset 'Auto-Mute Mode' Disabled 2>/dev/null || true
    echo "Audio: PCH card ${PCH_CARD} configured"
else
    echo "Audio: WARNING — PCH card not found, configure manually"
fi

# --- 10. Remove gnome-keyring (causes login prompts) ---
apt-get remove -y gnome-keyring 2>/dev/null || true

# --- 11. Disable screen blanking / power management ---
echo ""
echo "--- Disabling screen blanking ---"
DBUS=unix:path=/run/user/1000/bus
sudo -u ${FOSSIL_USER} DBUS_SESSION_BUS_ADDRESS=$DBUS gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
sudo -u ${FOSSIL_USER} DBUS_SESSION_BUS_ADDRESS=$DBUS gsettings set org.gnome.desktop.screensaver idle-activation-enabled false 2>/dev/null || true
sudo -u ${FOSSIL_USER} DBUS_SESSION_BUS_ADDRESS=$DBUS gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
sudo -u ${FOSSIL_USER} DBUS_SESSION_BUS_ADDRESS=$DBUS gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
sudo -u ${FOSSIL_USER} DBUS_SESSION_BUS_ADDRESS=$DBUS gsettings set org.gnome.settings-daemon.plugins.power idle-dim false 2>/dev/null || true

# Autostart script to disable DPMS on every login
mkdir -p /home/${FOSSIL_USER}/.config/autostart
cat > /home/${FOSSIL_USER}/.config/autostart/disable-blanking.desktop << EOFAUTO
[Desktop Entry]
Type=Application
Name=Disable Screen Blanking
Exec=bash -c "xset s off; xset -dpms; xset s noblank"
Hidden=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOFAUTO
chown -R ${FOSSIL_USER}:${FOSSIL_USER} /home/${FOSSIL_USER}/.config/autostart

# --- 12. Disable USB autosuspend (Kinect reliability) ---
echo ""
echo "--- Disabling USB autosuspend ---"
if ! grep -q 'usbcore.autosuspend=-1' /etc/default/grub; then
    sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 usbcore.autosuspend=-1"/' /etc/default/grub
    update-grub
fi

# --- 13. Tailscale (remote access from anywhere) ---
echo ""
echo "--- Installing Tailscale ---"
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "Run 'sudo tailscale up' to authenticate after setup"
fi

# --- 14. Healthchecks.io monitoring ---
echo ""
echo "--- Setting up health check ping ---"
HEALTHCHECK_URL="https://hc-ping.com/61821f08-8e12-4e92-bda2-0a406a63fe38"
cat > /etc/cron.d/healthcheck << EOF
# Ping healthchecks.io every 5 minutes
*/5 * * * * ${FOSSIL_USER} curl -fsS -m 10 --retry 5 ${HEALTHCHECK_URL} > /dev/null 2>&1
EOF
chmod 644 /etc/cron.d/healthcheck

# --- 15. Environmental monitoring ---
echo ""
echo "--- Setting up environmental monitoring ---"
cat > /etc/cron.d/env-check << EOF
# Environmental monitoring every minute
* * * * * ${FOSSIL_USER} ${FOSSIL_DIR}/env-check.sh > /dev/null 2>&1
EOF
chmod 644 /etc/cron.d/env-check
chmod +x "${FOSSIL_DIR}/env-check.sh"

# --- 16. Passwordless service control for pj-monitor ---
cat > /etc/sudoers.d/fossil-service << EOF
${FOSSIL_USER} ALL=(ALL) NOPASSWD: /bin/systemctl start fossil-app.service
${FOSSIL_USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop fossil-app.service
EOF
chmod 440 /etc/sudoers.d/fossil-service

# --- 17. Boot notification ---
cat > /etc/systemd/system/boot-notify.service << EOF
[Unit]
Description=Pushover boot notification
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${FOSSIL_USER}
ExecStart=/bin/bash -c 'sleep 10 && curl -fsS -m 10 -X POST https://api.pushover.net/1/messages.json --data-urlencode "token=avmrkpiuza87mcofkwn98s5prd2ukn" --data-urlencode "user=umhhs2kiz34wq91k76a1skjrq6vft5" --data-urlencode "title=Fossil NUC" --data-urlencode "message=System booted at \$(date)" --data-urlencode "priority=0"'

[Install]
WantedBy=multi-user.target
EOF
systemctl enable boot-notify.service

# --- 18. Shutdown notification ---
cat > /etc/systemd/system/shutdown-notify.service << EOF
[Unit]
Description=Pushover shutdown notification
DefaultDependencies=no
Before=shutdown.target reboot.target halt.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'curl -fsS -m 10 -X POST https://api.pushover.net/1/messages.json --data-urlencode "token=avmrkpiuza87mcofkwn98s5prd2ukn" --data-urlencode "user=umhhs2kiz34wq91k76a1skjrq6vft5" --data-urlencode "title=Fossil NUC" --data-urlencode "message=System shutting down at \$(date)" --data-urlencode "priority=0"'

[Install]
WantedBy=halt.target reboot.target shutdown.target
EOF
systemctl enable shutdown-notify.service

# --- 19. GRUB: skip menu on unclean shutdown ---
if ! grep -q 'GRUB_RECORDFAIL_TIMEOUT' /etc/default/grub; then
    echo 'GRUB_RECORDFAIL_TIMEOUT=0' >> /etc/default/grub
    update-grub
fi

# --- 20. WiFi keepalive (prevent router ARP expiry) ---
cat > /etc/cron.d/wifi-keepalive << 'EOF'
# Ping gateway every minute to keep ARP table fresh on router
* * * * * martin GW=$(ip route | awk '/default.*wlo1/{print $3}'); [ -n "$GW" ] && ping -c 1 -W 2 $GW > /dev/null 2>&1
EOF
chmod 644 /etc/cron.d/wifi-keepalive

# --- 21. Force WiFi to 5GHz band (avoid 6GHz driver quirks) ---
echo ""
echo "--- Configuring WiFi band ---"
nmcli connection show 2>/dev/null | grep -q "ANY" && \
    nmcli connection modify "ANY 1" 802-11-wireless.band a 2>/dev/null || true

# --- 22. Private network (wired 10.0.0.x) ---
echo ""
echo "--- Configuring private network ---"
cat > /etc/hosts << EOF
127.0.0.1 localhost
127.0.1.1 nuc

# Museum private network
10.0.0.1  fossil
10.0.0.2  ezoutlet
10.0.0.10 fossil-pj
10.0.0.11 haste-pj
EOF

# Set static IP on wired interface
nmcli connection show "Wired connection 1" &>/dev/null && \
    nmcli connection modify "Wired connection 1" ipv4.method manual ipv4.addresses 10.0.0.1/24 ipv4.gateway "" ipv4.dns "" 2>/dev/null || true

# --- 23. NUT UPS monitoring (notifications only, no shutdown) ---
echo ""
echo "--- Configuring UPS monitoring ---"
if dpkg -l nut &>/dev/null; then
    sed -i 's/MODE=none/MODE=standalone/' /etc/nut/nut.conf 2>/dev/null || true

    # Notify script for instant Pushover alerts on power events
    cat > /usr/local/bin/nut-notify.sh << 'NUTEOF'
#!/bin/bash
PO_TOKEN="avmrkpiuza87mcofkwn98s5prd2ukn"
PO_USER="umhhs2kiz34wq91k76a1skjrq6vft5"
curl -fsS -m 10 -X POST https://api.pushover.net/1/messages.json \
    --data-urlencode "token=${PO_TOKEN}" --data-urlencode "user=${PO_USER}" \
    --data-urlencode "title=Fossil UPS" --data-urlencode "message=$NOTIFYTYPE: $UPSNAME" \
    --data-urlencode "priority=1" --data-urlencode "sound=siren" > /dev/null 2>&1
NUTEOF
    chmod 755 /usr/local/bin/nut-notify.sh

    # upsmon: notifications only, shutdown disabled
    cat > /etc/nut/upsmon.conf << 'EOF'
MONITOR ups@localhost 1 admin fossil master
SHUTDOWNCMD "/bin/true"
POLLFREQ 5
POLLFREQALERT 5
HOSTSYNC 15
DEADTIME 15
RBWARNTIME 43200
NOCOMMWARNTIME 300
FINALDELAY 5

NOTIFYCMD /usr/local/bin/nut-notify.sh

NOTIFYFLAG ONLINE    EXEC
NOTIFYFLAG ONBATT    EXEC
NOTIFYFLAG LOWBATT   EXEC
NOTIFYFLAG FSD       EXEC
NOTIFYFLAG COMMOK    EXEC
NOTIFYFLAG COMMBAD   EXEC
NOTIFYFLAG REPLBATT  EXEC
NOTIFYFLAG NOCOMM    EXEC

NOTIFYMSG ONLINE    "Power restored - on mains"
NOTIFYMSG ONBATT    "Power loss - on battery"
NOTIFYMSG LOWBATT   "Battery low - critical"
NOTIFYMSG FSD       "Forced shutdown"
NOTIFYMSG COMMOK    "UPS communication restored"
NOTIFYMSG COMMBAD   "UPS communication lost"
NOTIFYMSG REPLBATT  "Battery needs replacement"
NOTIFYMSG NOCOMM    "UPS not responding"
EOF

    systemctl enable nut-server.service nut-driver@ups.service nut-monitor.service 2>/dev/null || true
fi

# --- 24. Log rotation ---
cat > /etc/logrotate.d/fossil << 'EOF'
/var/log/fossil.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
    size 10M
}
EOF

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Copy fossil project files to ${FOSSIL_DIR}"
echo "  2. Copy assets (fossil.png, fossil2.png, audio/) to ${FOSSIL_DIR}/assets/"
echo "  3. Run 'sudo tailscale up' to authenticate Tailscale"
echo "  4. Reboot: sudo reboot"
echo "  5. Fossil will auto-start after login"
