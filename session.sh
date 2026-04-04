#!/bin/bash
# Fossil kiosk session — no desktop environment
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
