#!/bin/bash
# Environmental monitoring for museum installation
# Pings healthchecks.io with warnings, reboots on Kinect depth camera failure
HC_URL="https://hc-ping.com/1628f9ee-7fdf-4cc7-bf3b-7a240450227d"
PO_TOKEN="avmrkpiuza87mcofkwn98s5prd2ukn"
PO_USER="umhhs2kiz34wq91k76a1skjrq6vft5"
REBOOT_STATE="/tmp/fossil-kinect-reboot"
KINECT_FAIL_COUNT="/tmp/fossil-kinect-fails"
MAX_REBOOTS=3           # max reboots per window
REBOOT_WINDOW=3600      # 1 hour window (seconds)
MIN_UPTIME=300          # don't reboot if up < 5 min (let boot settle)
CONSEC_FAILS_NEEDED=1   # reboot on first check after boot settle
WARNINGS=""

# Helper: send Pushover notification
pushover() {
    local title="$1" msg="$2" pri="${3:-0}"
    curl -fsS -m 10 -X POST https://api.pushover.net/1/messages.json \
        -d "token=${PO_TOKEN}" -d "user=${PO_USER}" \
        -d "title=${title}" -d "message=${msg}" \
        -d "priority=${pri}" -d "sound=siren" > /dev/null 2>&1
}

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

# --- Kinect depth camera present (with auto-reboot) ---
if ! lsusb | grep -q '045e:097c'; then
    UPTIME_SEC=$(awk '{print int($1)}' /proc/uptime)
    NOW=$(date +%s)

    # Track consecutive failures
    FAILS=1
    if [ -f "$KINECT_FAIL_COUNT" ]; then
        FAILS=$(( $(cat "$KINECT_FAIL_COUNT") + 1 ))
    fi
    echo "$FAILS" > "$KINECT_FAIL_COUNT"

    # Count recent reboots within window
    RECENT=0
    if [ -f "$REBOOT_STATE" ]; then
        while read ts; do
            [ $((NOW - ts)) -lt $REBOOT_WINDOW ] && RECENT=$((RECENT + 1))
        done < "$REBOOT_STATE"
    fi

    if [ "$UPTIME_SEC" -lt "$MIN_UPTIME" ]; then
        # Just booted — too early to act, don't count as a fail
        echo "0" > "$KINECT_FAIL_COUNT"
        WARNINGS="${WARNINGS}KINECT: Depth camera missing (boot settling, uptime ${UPTIME_SEC}s)\n"
        if [ "$FAILS" -eq 1 ]; then
            pushover "Fossil NUC" "Kinect depth camera not detected after boot (uptime ${UPTIME_SEC}s). Waiting to settle..." 0
        fi
    elif [ "$FAILS" -lt "$CONSEC_FAILS_NEEDED" ]; then
        # Not enough consecutive failures yet
        WARNINGS="${WARNINGS}KINECT: Depth camera missing (check ${FAILS}/${CONSEC_FAILS_NEEDED})\n"
        if [ "$FAILS" -eq 1 ]; then
            pushover "Fossil NUC" "Kinect depth camera missing. Monitoring... (${FAILS}/${CONSEC_FAILS_NEEDED} checks)" 0
        fi
    elif [ "$RECENT" -ge "$MAX_REBOOTS" ]; then
        # Reboot limit reached — give up, just alert
        WARNINGS="${WARNINGS}KINECT: Depth camera missing (reboot limit reached: ${RECENT}/${MAX_REBOOTS} in last hour)\n"
        if [ "$FAILS" -eq "$CONSEC_FAILS_NEEDED" ]; then
            pushover "Fossil NUC Alert" "Kinect depth camera still missing after ${RECENT} reboots. Manual intervention needed." 1
        fi
    else
        # Reboot to re-enumerate USB
        WARNINGS="${WARNINGS}KINECT: Depth camera missing — REBOOTING (attempt $((RECENT + 1))/${MAX_REBOOTS})\n"
        echo "$NOW" >> "$REBOOT_STATE"
        # Prune old entries
        tmpf=$(mktemp)
        while read ts; do
            [ $((NOW - ts)) -lt $REBOOT_WINDOW ] && echo "$ts"
        done < "$REBOOT_STATE" > "$tmpf"
        mv "$tmpf" "$REBOOT_STATE"
        pushover "Fossil NUC Reboot" "Kinect depth camera missing after ${FAILS} checks. Rebooting (attempt $((RECENT + 1))/${MAX_REBOOTS})." 1
        sudo reboot
        exit 0
    fi
else
    # Camera present — clear fail counter
    if [ -f "$KINECT_FAIL_COUNT" ]; then
        rm -f "$KINECT_FAIL_COUNT"
    fi
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
    # Non-Kinect warnings get a Pushover alert (Kinect handles its own above)
    NON_KINECT=$(echo -e "$WARNINGS" | grep -v KINECT)
    if [ -n "$NON_KINECT" ]; then
        pushover "Fossil NUC Alert" "$NON_KINECT" 1
    fi
else
    curl -fsS -m 10 "${HC_URL}" > /dev/null 2>&1
fi
