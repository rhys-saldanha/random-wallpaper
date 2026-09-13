# random-wallpaper

Picks a random photo (or a few, stacked side by side) from your photo
library, crops it to fill your screen at whatever aspect ratio it is, and
sets it as the GNOME wallpaper and lock screen background. Includes a
top-bar indicator showing which photo(s) are currently set, with a button
to reroll.

## Requirements

Core (the script exits with a clear message telling you exactly what's
missing and its Debian/Ubuntu package name if any of these aren't on
`PATH` -- nothing here gets installed automatically, that's on you):

| Tool | Debian/Ubuntu package | Used for |
|---|---|---|
| `xrandr` | `x11-xserver-utils` | detecting your screen resolution |
| `magick`, `identify` | `imagemagick` | cropping, resizing, compositing |
| `gsettings` | `libglib2.0-bin` | actually setting the wallpaper (GNOME) |

```
sudo apt install x11-xserver-utils imagemagick libglib2.0-bin
```

Optional:

| Tool | Debian/Ubuntu package | Used for |
|---|---|---|
| `exiftool` | `libimage-exiftool-perl` | EXIF capture dates + image dimensions for `scan.py`'s photo cache (drives grouping and the tray-menu dates). Optional -- scan.py falls back to file mtime and `identify` if it's missing |
| `notify-send` | `libnotify-bin` | a desktop notification if a plugin is enabled but its tools aren't installed -- silently skipped if missing |
| `opencode` | see [opencode.ai](https://opencode.ai) | only if you enable the `vision` plugin (see below) |

GNOME Shell 45+ (uses the modern ESM extension API) and a systemd user
session are assumed for the timer/extension; the core script itself has no
GNOME-specific dependency beyond `gsettings`.

## Install

```
git clone <this repo> ~/git/random-wallpaper
~/git/random-wallpaper/install.sh
```

`install.sh` is safe to re-run (e.g. after editing a file, or moving the
repo). It:
- generates/symlinks two systemd user timers:
  `random-wallpaper.timer` (rerolls the wallpaper every 3 hours) and
  `random-wallpaper-scan.timer` (runs `scan.py` to rebuild the photo
  cache every 12 hours)
- symlinks the GNOME Shell extension into place and enables it (a **new**
  extension UUID needs one manual shell restart to be picked up the first
  time -- X11: `Alt+F2`, `r`, `Enter`; Wayland: log out and back in)

## Photo cache

To group photos that share a few days, the wallpaper picks use the *EXIF
capture date*, not file mtime. Reading EXIF and image dimensions fresh per
run would be slow on a big library, so `scan.py` (run by
`random-wallpaper-scan.timer`) builds a small sqlite database at
`~/.cache/random-wallpaper.db` mapping path -> (capture date, width,
height), and `random-wallpaper.py` only *reads* it: picking and stacking
never touches the library beyond rendering the few photos it actually
shows.

Extraction is one `exiftool` call per 200-photo chunk that reads
DateTimeOriginal, ImageSize and Orientation in a single header pass per
file (dimensions are corrected for EXIF orientation locally); chunks are
scanned in parallel, so even a first build over a network-mounted library
stays usable. Scans are incremental: a scan re-reads only files *newer
than the newest cached photo*. When the number of files on disk disagrees
with the cache (e.g. a photo restored with a very old mtime, or
deletions), the scan degrades to one complete sweep -- the per-file mtime
diff, which is authoritative -- and prunes rows for deleted files. Each
chunk commits and prints progress, so a first build can be interrupted and
resumed without losing work.

Files with no EXIF date fall back to their mtime; if `exiftool` is missing
entirely, `scan.py` uses mtime dates and `identify` dimensions, so the
cache still builds. The DB is disposable: delete it and the next scan
rebuilds it. `random-wallpaper.py` exits with a hint to run `scan.py`
until then.

`scan.py` needs no display and only touches the database.

## Configure

On first run, `random-wallpaper.py` creates a template config at
`~/.config/random-wallpaper/config.json`. Edit it before running without
`--photos`:

```json
{
  "photo_roots": ["/path/to/your/photo/library"],
  "plugins": {"vision": false}
}
```

- `photo_roots` is searched recursively at any depth, so one top-level
  library folder full of camera/trip subfolders works fine as a single
  entry. Only `.jpg`/`.jpeg` are picked up.
- `plugins.vision`: if `true`, a single photo gets cropped with the main
  subject kept in frame (via an `opencode` vision model call) instead of a
  plain centred crop. Off by default -- needs `opencode` installed and
  configured with a working model. If you enable it without `opencode`
  installed, the script exits with a clear error instead of silently
  falling back.

## Usage

- Runs automatically every 3 hours via the `random-wallpaper.timer`
  systemd user unit.
- Reroll on demand: click the tray icon (top bar) and choose "Reroll
  wallpaper", or just run `~/git/random-wallpaper/random-wallpaper.py`
  directly.
- Rebuild the photo cache (new photos show up faster than the 12h scan):
  `~/git/random-wallpaper/scan.py`
- Test specific photos without touching your config or waiting for a
  random pick: `random-wallpaper.py photo1.jpg photo2.jpg ...` -- renders
  exactly those as stacked panels. Useful for checking stacking/gap
  behaviour; see `--gap` too.

## Development

```
python3 -m unittest test_scan test_random_wallpaper test_plugins -v
node --test gnome-extension/parseSources.test.mjs
```

Both suites mock every system boundary (subprocess calls, the photo
library, GNOME's own JS modules aren't mockable so only the extension's
pure parsing logic is unit-tested) -- no display, NAS, or GNOME session
needed to run them.

To iterate on `gnome-extension/extension.js` without restarting your real
GNOME session, run `./dev-session.sh` -- it launches a nested GNOME Shell
(same trick used by [Tiling Shell](https://github.com/domferr/tilingshell))
with the extension already symlinked in via `install.sh`.
