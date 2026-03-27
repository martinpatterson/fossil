#!/bin/bash
# Auto-restart wrapper for Fossil installation.
# Restarts on exit code 75 (sensor failure). Exits on 0 (clean quit) or other codes.
cd "$(dirname "$0")"
source .venv/bin/activate

# Kill any stale fossil processes on startup
pkill -9 -f "python.*main.py" 2>/dev/null
sleep 2

while true; do
    echo "$(date): Starting Fossil..."
    python main.py
    EXIT_CODE=$?

    # Kill any zombies before restart
    pkill -9 -f "python.*main.py" 2>/dev/null
    sleep 1

    if [ $EXIT_CODE -eq 75 ]; then
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
