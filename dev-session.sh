#!/usr/bin/env bash
# Launches a nested GNOME Shell (Wayland) with this extension symlinked in,
# for fast iteration on gnome-extension/*.js without restarting your real
# session. Same trick used by github.com/domferr/tilingshell. Run install.sh
# first so the extension is symlinked into place, then just edit and relaunch
# this to see changes -- no Alt+F2/r needed since it's a whole separate shell.
set -euo pipefail

exec dbus-run-session -- env \
    MUTTER_DEBUG_NUM_DUMMY_MONITORS=1 \
    MUTTER_DEBUG_DUMMY_MODE_SPECS=1920x1080 \
    gnome-shell --nested --wayland
