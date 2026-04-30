#!/bin/bash
# Fossil kiosk X session — sets up display environment only
# App lifecycle is managed by pj-monitor.service → fossil-app.service

# Force 1920x1080 on whichever HDMI/DP is connected (4K hurts performance).
# Tries each output; only the connected one with a 1920x1080 mode succeeds.
for out in HDMI-1 HDMI-2 DP-1 DP-2 DP-3 DP-4; do
    xrandr --output "$out" --mode 1920x1080 2>/dev/null
done

# Disable ALL screen blanking and power management
xset s off
xset s noblank
xset s 0 0
xset -dpms
xset dpms 0 0 0

# Hide cursor
unclutter -idle 0.5 -root &

# Set background to white
xsetroot -solid black

# Start minimal window manager (handles focus, no decorations)
matchbox-window-manager -use_titlebar no -use_cursor no &

# Export display info for fossil-app.service
echo "DISPLAY=$DISPLAY" > /tmp/fossil-display-env
echo "XAUTHORITY=$XAUTHORITY" >> /tmp/fossil-display-env
chmod 644 /tmp/fossil-display-env

echo "$(date): X session ready (DISPLAY=$DISPLAY)"

# Keep session alive
while true; do
    sleep 3600
done
