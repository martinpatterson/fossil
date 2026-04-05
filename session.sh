#!/bin/bash
# Fossil kiosk session — minimal window manager for keyboard focus
# Disable ALL screen blanking and power management
xset s off
xset s noblank
xset s 0 0
xset -dpms
xset dpms 0 0 0

# Hide cursor
unclutter -idle 0.5 -root &

# Set background to white
xsetroot -solid white

# Start minimal window manager (handles focus, no decorations)
matchbox-window-manager -use_titlebar no -use_cursor no &

# Run fossil with auto-restart
cd /home/martin/fossil
source .venv/bin/activate

while true; do
    echo "$(date): Starting Fossil..."
    timeout 86400 python main.py
    EXIT_CODE=$?
    pkill -9 -f "python.*main.py" 2>/dev/null
    sleep 1

    if [ $EXIT_CODE -eq 124 ]; then
        echo "$(date): Scheduled restart (24h)..."
        sleep 3
    elif [ $EXIT_CODE -eq 75 ]; then
        echo "$(date): Sensor failure, restarting in 5s..."
        sleep 4
    else
        echo "$(date): Exit ($EXIT_CODE), restarting in 10s..."
        sleep 9
    fi
done
