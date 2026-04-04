#!/bin/bash
# Wait for display to be ready, then start fossil
export DISPLAY=:0

# Wait for XAUTHORITY file to appear (up to 60 seconds)
for i in $(seq 1 60); do
    XAUTH=$(ls /run/user/1000/.mutter-Xwaylandauth.* /run/user/1000/gdm/Xauthority 2>/dev/null | head -1)
    if [ -n "$XAUTH" ]; then
        export XAUTHORITY="$XAUTH"
        echo "$(date): Found XAUTHORITY: $XAUTH (after ${i}s)"
        break
    fi
    sleep 1
done

if [ -z "$XAUTHORITY" ]; then
    echo "$(date): WARNING — no XAUTHORITY found after 60s"
fi

exec /home/martin/fossil/run.sh
