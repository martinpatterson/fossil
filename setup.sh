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
    rplidar-roboticia

# --- 8. Systemd service ---
echo ""
echo "--- Installing systemd service ---"
cat > /etc/systemd/system/fossil.service << EOF
[Unit]
Description=Fossil Interactive Installation
After=graphical.target
Wants=graphical.target

[Service]
Type=simple
User=${FOSSIL_USER}
Group=${FOSSIL_USER}
WorkingDirectory=${FOSSIL_DIR}
Environment=DISPLAY=:0
ExecStartPre=/bin/sleep 5
ExecStart=/bin/bash -c 'export XAUTHORITY=\$(ls /run/user/1000/.mutter-Xwaylandauth.* /run/user/1000/gdm/Xauthority 2>/dev/null | head -1); exec ${FOSSIL_DIR}/run.sh'
Restart=always
RestartSec=10
StandardOutput=append:/var/log/fossil.log
StandardError=append:/var/log/fossil.log

ProtectSystem=false
ReadWritePaths=${FOSSIL_DIR} /tmp /var/log /dev

[Install]
WantedBy=graphical.target
EOF

touch /var/log/fossil.log
chown ${FOSSIL_USER}:${FOSSIL_USER} /var/log/fossil.log
chmod +x "${FOSSIL_DIR}/run.sh"

systemctl daemon-reload
systemctl enable fossil.service

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

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Copy fossil project files to ${FOSSIL_DIR}"
echo "  2. Copy assets (fossil.png, fossil2.png, audio/) to ${FOSSIL_DIR}/assets/"
echo "  3. Reboot: sudo reboot"
echo "  4. Fossil will auto-start after login"
