#!/bin/bash
# Environmental monitoring for museum installation
# Pings healthchecks.io with warnings (does NOT shut down or take action)
HC_URL="https://hc-ping.com/1628f9ee-7fdf-4cc7-bf3b-7a240450227d"
PO_TOKEN="avmrkpiuza87mcofkwn98s5prd2ukn"
PO_USER="umhhs2kiz34wq91k76a1skjrq6vft5"
WARNINGS=""

# --- CPU temperature (zone 1 = x86_pkg_temp) ---
TEMP=$(cat /sys/class/thermal/thermal_zone1/temp 2>/dev/null)
TEMP_C=$((TEMP / 1000))
if [ "$TEMP_C" -ge 85 ]; then
    WARNINGS="${WARNINGS}OVERHEAT: CPU at ${TEMP_C}C\n"
fi

# --- UPS power status ---
UPS_STATUS=$(upsc ups ups.status 2>/dev/null)
if echo "$UPS_STATUS" | grep -q "OB"; then
    BATT=$(upsc ups battery.charge 2>/dev/null)
    WARNINGS="${WARNINGS}POWER LOSS: UPS on battery (${BATT}%)\n"
fi

# --- Disk space (warn at 90%) ---
DISK_PCT=$(df / --output=pcent | tail -1 | tr -d ' %')
if [ "$DISK_PCT" -ge 90 ]; then
    WARNINGS="${WARNINGS}DISK: ${DISK_PCT}% full\n"
fi

# --- Kinect depth camera present ---
if ! lsusb | grep -q '045e:097c'; then
    WARNINGS="${WARNINGS}KINECT: Depth camera not detected\n"
fi

# --- WiFi signal (warn below -70 dBm) ---
SIGNAL=$(iwconfig wlo1 2>/dev/null | grep -oP 'Signal level=\K-?[0-9]+')
if [ -n "$SIGNAL" ] && [ "$SIGNAL" -le -70 ]; then
    WARNINGS="${WARNINGS}WIFI: Weak signal (${SIGNAL} dBm)\n"
fi

# --- App running ---
if ! pgrep -f 'python.*main.py' > /dev/null; then
    WARNINGS="${WARNINGS}APP: Fossil not running\n"
fi

# --- Send result ---
if [ -n "$WARNINGS" ]; then
    MSG=$(echo -e "$WARNINGS")
    echo -e "$WARNINGS" | curl -fsS -m 10 "${HC_URL}/fail" --data-binary @- > /dev/null 2>&1
    # Pushover direct notification with detail
    curl -fsS -m 10 -X POST https://api.pushover.net/1/messages.json \
        -d "token=${PO_TOKEN}" \
        -d "user=${PO_USER}" \
        -d "title=Fossil NUC Alert" \
        -d "message=${MSG}" \
        -d "priority=1" \
        -d "sound=siren" > /dev/null 2>&1
else
    curl -fsS -m 10 "${HC_URL}" > /dev/null 2>&1
fi
