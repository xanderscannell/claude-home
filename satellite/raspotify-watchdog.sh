#!/bin/bash
# Restart raspotify when librespot loses its Spotify session and hasn't logged in again within GRACE seconds.
# librespot 0.8.0 only notices a closed session when the next command arrives (that command is lost), and
# sometimes it never recovers; a restart logs in again in about 5 s. Needs librespot's info log (no
# LIBRESPOT_QUIET in /etc/raspotify/conf), where "Authenticated as" appears.
#
# Try it without restarting anything:
#   TAG=lab RESTART="echo restart" GRACE=3 ./raspotify-watchdog.sh
#   logger -t lab "Connection to server closed."    (then also: logger -t lab "Authenticated as x")
TAG=${TAG:-librespot}
GRACE=${GRACE:-30}
RESTART=${RESTART:-systemctl restart raspotify}

journalctl -t "$TAG" -f -n 0 -o cat | while read -r line; do
    [[ $line == *"Connection to server closed"* ]] || continue
    closed=$(date +%s)
    sleep "$GRACE"
    if journalctl -t "$TAG" --since "@$closed" -o cat | grep -q "Authenticated as"; then
        echo "session closed; librespot logged in again by itself"
    else
        echo "session closed and not back after ${GRACE}s; restarting raspotify"
        $RESTART
    fi
done
