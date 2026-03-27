#!/bin/bash
# Auto-restart wrapper for Fossil installation.
# Restarts on exit code 75 (sensor failure). Exits on 0 (clean quit) or other codes.
cd "$(dirname "$0")"
source .venv/bin/activate

while true; do
    echo "$(date): Starting Fossil..."
    python main.py
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 75 ]; then
        echo "$(date): Sensor failure (exit 75), restarting in 5s..."
        sleep 5
    elif [ $EXIT_CODE -eq 0 ]; then
        echo "$(date): Clean exit."
        break
    else
        echo "$(date): Unexpected exit ($EXIT_CODE), restarting in 10s..."
        sleep 10
    fi
done
