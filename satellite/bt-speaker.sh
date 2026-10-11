#!/bin/sh
# Plays the loopback's mix (everything sent to ALSA "default"; see asound.conf) on the Bluetooth speaker at
# BT_SPEAKER, connecting to it first when it isn't connected. When the speaker goes away the stream ends, and
# the loop waits for it to come back; the satellite and raspotify keep playing into the loopback meanwhile.
# The format matches asound.conf's dmix onto the loopback. Silence is sent while nothing plays, so the
# speaker stays awake and the first word isn't lost to the stream starting.
#
# Pair the speaker once (in pairing mode; an agent is needed, so run it interactively):
#   bluetoothctl   then: agent NoInputNoOutput, default-agent, scan on, pair <address>, trust <address>
: "${BT_SPEAKER:?set BT_SPEAKER to the speaker's Bluetooth address}"
FORMAT="-f S16_LE -r 48000 -c 2 -t raw --buffer-time=100000"

while true; do
    if ! bluetoothctl info "$BT_SPEAKER" | grep -q "Connected: yes" &&
       ! bluetoothctl connect "$BT_SPEAKER" >/dev/null 2>&1; then
        sleep 10
        continue
    fi
    echo "playing on $BT_SPEAKER"
    # shellcheck disable=SC2086
    arecord -q -D hw:Loopback,1,0 $FORMAT | aplay -q -D "bluealsa:DEV=$BT_SPEAKER,PROFILE=a2dp" $FORMAT
    echo "speaker stream ended; waiting for the speaker"
    sleep 2
done
