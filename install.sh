#!/usr/bin/env bash
# Wires this repo's systemd units and GNOME extension into place and
# enables them. Safe to re-run after editing any of the source files here
# (or after moving the repo -- the service file is regenerated with the
# current path each time).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_UUID="random-wallpaper@rhyssaldanha.org"

mkdir -p ~/.config/systemd/user
rm -f ~/.config/systemd/user/random-wallpaper.service      # may be stale symlinks from an older install
rm -f ~/.config/systemd/user/random-wallpaper-scan.service
for name in random-wallpaper random-wallpaper-scan; do
    sed "s|@REPO_DIR@|$REPO_DIR|" "$REPO_DIR/systemd/$name.service" > ~/.config/systemd/user/$name.service
    ln -sf "$REPO_DIR/systemd/$name.timer" ~/.config/systemd/user/$name.timer
done
systemctl --user daemon-reload
systemctl --user enable --now random-wallpaper.timer
systemctl --user enable --now random-wallpaper-scan.timer

mkdir -p ~/.local/share/gnome-shell/extensions
ln -sfn "$REPO_DIR/gnome-extension" ~/.local/share/gnome-shell/extensions/"$EXT_UUID"
gnome-extensions enable "$EXT_UUID" \
    || echo "Could not enable the extension automatically -- enable '$EXT_UUID' via the Extensions app." >&2

echo "Timer: $(systemctl --user is-enabled random-wallpaper.timer 2>&1)"
echo "Scan timer: $(systemctl --user is-enabled random-wallpaper-scan.timer 2>&1)"
echo "Extension: $(gnome-extensions info "$EXT_UUID" 2>&1 | grep -i '^ *state:' || echo 'not detected yet')"
