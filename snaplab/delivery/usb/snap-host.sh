#!/bin/sh
# bin/snap on the stick and the host: the SNAP engine's command (Design 0001, section 9).
# The engine is plain Python (standard library only), shipped in /var/lib/snap/lib.
if ! command -v python3 >/dev/null 2>&1; then
    echo "SNAP: python3 is missing on this host, so setup cannot continue." | tee /dev/console >&2
    exit 1
fi
PYTHONPATH=/var/lib/snap/lib PYTHONDONTWRITEBYTECODE=1 exec python3 -m snaplab.hostcli "$@"
