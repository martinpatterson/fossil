#!/bin/bash
# Auto-restart wrapper for Fossil installation.
# Restarts on exit code 75 (sensor failure). Exits on 0 (clean quit).
# Preemptive restart every RESTART_HOURS to avoid memory leak degradation.
cd "$(dirname "$0")"
source .venv/bin/activate

RESTART_HOURS=24
RESTART_SECS=$((RESTART_HOURS * 3600))

# Kill any stale fossil processes on startup
pkill -9 -f "python.*main.py" 2>/dev/null
sleep 2

while true; do
    echo "$(date): Starting Fossil (will restart after ${RESTART_HOURS}h)..."
    timeout $RESTART_SECS python main.py
    EXIT_CODE=$?

    # Kill any zombies before restart
    pkill -9 -f "python.*main.py" 2>/dev/null
    sleep 1

    if [ $EXIT_CODE -eq 124 ]; then
        echo "$(date): Scheduled restart (${RESTART_HOURS}h)..."
        sleep 3
    elif [ $EXIT_CODE -eq 75 ]; then
        echo "$(date): Sensor failure (exit 75), restarting in 5s..."
        sleep 4
    elif [ $EXIT_CODE -eq 0 ]; then
        echo "$(date): Clean exit."
        break
    else
        echo "$(date): Unexpected exit ($EXIT_CODE), restarting in 10s..."
        sleep 9
    fi
done
