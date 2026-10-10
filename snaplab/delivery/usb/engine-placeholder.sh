#!/bin/sh
# Placeholder engine entry point (bin/snap on the stick) until the engine
# exists (milestone M2). It proves the handoff reached the host.
case "${1:-}" in
    resume)
        echo "SNAP: handoff reached the host. CRACKLE is not implemented yet (milestone M2)." | tee /dev/console
        ;;
    *)
        echo "usage: snap resume" >&2
        exit 2
        ;;
esac
