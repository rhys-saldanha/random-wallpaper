#!/usr/bin/env python3
"""Pick a random photo from your configured photo library and set it as the
GNOME wallpaper and lock screen background, sized to fit any screen (built
for an ultrawide, but works on any aspect ratio).

On first run this creates a template config at
~/.config/random-wallpaper/config.json -- edit 'photo_roots' there to point
at your photo folder(s) before running without --photos.

A single photo is cropped to fill the whole screen: centred by default, or
with the main subject kept in frame if the 'vision' plugin is enabled under
'plugins' in the config (off by default -- it needs opencode installed and
configured; see plugins.py). If one photo isn't enough, companions whose
*EXIF capture date* falls within a few days of the anchor's -- any
orientation -- keep getting added at their natural, uncropped size until the
screen is filled or nearly filled; whatever width is left over becomes even
gutters between and around the panels, like flexbox 'space-evenly'.

Capture dates and photo dimensions are cached in a small sqlite database at
~/.cache/random-wallpaper.db. The separate scan.py script (run by the
random-wallpaper-scan.timer) builds and owns that cache; this script only
reads it, so every wallpaper run stays fast even over a slow NAS.

Run periodically via the random-wallpaper.timer systemd --user unit.
"""
import argparse
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from plugins import enabled_plugin
from scan import CACHE_DB, CONFIG_FILE, image_size, load_config

OUT = Path.home() / ".cache" / "random-wallpaper.jpg"
SOURCE_FILE = Path.home() / ".cache" / "random-wallpaper-source.txt"
SUBJECT_MARGIN = 1.15  # breathing room kept around the detected subject
NEARBY_SECONDS = 3 * 86400  # "a few days" window for companions
FILL_RATIO = 0.9  # stop adding companions once this much of the width is used
# Core tools this script always needs, with their Debian/Ubuntu package name
# as an install hint. exiftool is deliberately not here -- capture dates fall
# back to file mtime gracefully if it's missing. Any plugin-specific tools
# (e.g. opencode for the vision plugin) are checked separately by
# plugins.enabled_plugin().
REQUIRED_TOOLS = {
    "xrandr": "x11-xserver-utils",
    "magick": "imagemagick",
    "identify": "imagemagick",
    "gsettings": "libglib2.0-bin",
}


def check_requirements(needs_xrandr: bool) -> None:
    """Exits with a clear message listing exactly what's missing if any
    core tool isn't on PATH. Does not install anything itself -- that
    usually needs sudo and package names vary by distro/package manager,
    so installing is left to you. xrandr is only required when the
    'screen_size' config isn't set (see screen_size())."""
    tools = dict(REQUIRED_TOOLS)
    if not needs_xrandr:
        tools.pop("xrandr")
    missing = {t: pkg for t, pkg in tools.items() if shutil.which(t) is None}
    if not missing:
        return
    lines = "\n".join(f"  {t}  (Debian/Ubuntu: apt install {pkg})" for t, pkg in missing.items())
    sys.exit(f"Missing required tool(s):\n{lines}\nInstall them, then run this again.")


def screen_size(config: dict) -> tuple[int, int]:
    """Screen resolution: the optional 'screen_size' config takes priority
    (e.g. {"width": 5120, "height": 1440}) so headless runs don't need a
    display or xrandr; otherwise the current resolution is probed via
    xrandr, which needs a connected display."""
    cfg = config.get("screen_size")
    if isinstance(cfg, dict) and cfg.get("width") and cfg.get("height"):
        return int(cfg["width"]), int(cfg["height"])
    out = subprocess.run(["xrandr"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if " connected" in line:
            for tok in line.split():
                if "x" in tok and tok[0].isdigit():
                    w, h = tok.split("+")[0].split("x")
                    return int(w), int(h)
    raise RuntimeError("no connected display found in xrandr output")


def relative_label(path: Path, photo_base: Path) -> str:
    """Human-friendly path relative to photo_base, e.g.
    'Camera1/2024 Trip/DSCF3496.JPG'."""
    try:
        return str(path.relative_to(photo_base))
    except ValueError:
        return str(path)


def dates_taken(paths: list[Path]) -> list[str]:
    """EXIF DateTimeOriginal for each photo, formatted like '18 Mar 2026'.
    Falls back to file mtime if exiftool is missing or a tag is blank."""
    raw = [""] * len(paths)
    try:
        out = subprocess.run(
            ["exiftool", "-T", "-DateTimeOriginal", *map(str, paths)],
            capture_output=True, text=True, timeout=10,
        ).stdout.splitlines()
        if len(out) == len(paths):
            raw = out
    except (subprocess.TimeoutExpired, OSError):
        pass

    dates = []
    for p, r in zip(paths, raw):
        try:
            dates.append(datetime.strptime(r.strip(), "%Y:%m:%d %H:%M:%S").strftime("%d %b %Y"))
        except ValueError:
            dates.append(datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %Y"))
    return dates


def load_cache() -> dict[str, tuple[float, int, int]]:
    """path -> (capture epoch, w, h) for every photo scan.py has cached.
    Read-only connection: scan.py owns the database, the wallpaper never
    writes to it, so it can't corrupt or wedge it."""
    try:
        conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
        try:
            return {
                row[0]: (row[1], row[2], row[3])
                for row in conn.execute("SELECT path, taken, w, h FROM photos")
            }
        finally:
            conn.close()
    except sqlite3.Error:
        raise RuntimeError(
            f"photo cache at {CACHE_DB} is missing or unreadable; "
            f"random-wallpaper-scan.timer builds it (or run scan.py)"
        ) from None


def natural_width(path: Path, screen_h: int) -> int:
    """Width of the photo (any orientation) scaled to the screen height,
    with no cropping -- its full, unmodified composition."""
    w, h = image_size(path)
    return round(w / h * screen_h)


def pick_group(cache: dict[str, tuple[float, int, int]], screen_w: int, screen_h: int) -> list[tuple[Path, int]]:
    """Pick a random anchor photo, then add same-date companions (EXIF
    capture date within NEARBY_SECONDS of the anchor's, any orientation) in
    random order at their natural width while they fit, stopping once the
    screen is filled or nearly filled (FILL_RATIO). A photo that doesn't
    fit the leftover width still gets taken by swapping out the narrowest
    panel already added (if that fits and fills more), so a wide photo
    shuffled late doesn't leave an avoidable big gap. The screen width is
    the only bound on how many fit; leftover width becomes gaps. Returns
    (path, natural width at screen height) pairs so the caller doesn't
    re-probe sizes."""
    rows = list(cache.items())
    anchor, (anchor_taken, aw, ah) = random.choice(rows)
    pool = [
        (Path(p), round(w / h * screen_h))
        for p, (t, w, h) in rows
        if p != anchor and abs(t - anchor_taken) <= NEARBY_SECONDS
    ]
    random.shuffle(pool)

    group = [(Path(anchor), round(aw / ah * screen_h))]
    total = group[0][1]
    for p, width in pool:
        if total >= screen_w * FILL_RATIO:
            break
        if width <= screen_w - total:
            group.append((p, width))
            total += width
            continue
        smallest, smallest_i = min((w, i) for i, (_, w) in enumerate(group[1:], 1)) if len(group) > 1 else (0, None)
        if smallest_i is not None and width > smallest and width - smallest <= screen_w - total:
            group[smallest_i] = (p, width)
            total += width - smallest
    return group


def compute_crop(img_w: int, img_h: int, bbox, target_ratio: float) -> tuple[int, int, int, int]:
    """Crop as tightly as possible towards target_ratio while keeping the
    whole subject box in frame. Only one axis is ever trimmed: width for
    already-wide photos, height for the (usual) narrower-than-screen case."""
    l, t, r, b = bbox
    sub_w, sub_h = (r - l) * img_w, (b - t) * img_h
    sub_cx, sub_cy = (l + r) / 2 * img_w, (t + b) / 2 * img_h
    native_ratio = img_w / img_h

    if native_ratio >= target_ratio:
        crop_h = img_h
        crop_w = min(max(crop_h * target_ratio, sub_w * SUBJECT_MARGIN), img_w)
        cx = min(max(sub_cx, crop_w / 2), img_w - crop_w / 2)
        x0, y0 = cx - crop_w / 2, 0
    else:
        crop_w = img_w
        crop_h = min(max(crop_w / target_ratio, sub_h * SUBJECT_MARGIN), img_h)
        cy = min(max(sub_cy, crop_h / 2), img_h - crop_h / 2)
        x0, y0 = 0, cy - crop_h / 2

    return int(x0), int(y0), int(crop_w), int(crop_h)


def render_panel(photo: Path, panel_w: int, panel_h: int, dest: Path, plugin) -> None:
    img_w, img_h = image_size(photo)
    bbox = plugin.subject_bbox(photo) if plugin else (0.1, 0.1, 0.9, 0.9)
    x0, y0, cw, ch = compute_crop(img_w, img_h, bbox, panel_w / panel_h)
    subprocess.run(
        [
            "magick", str(photo), "-auto-orient",
            "-crop", f"{cw}x{ch}+{x0}+{y0}", "+repage",
            "-resize", f"{panel_w}x{panel_h}",
            "-gravity", "center", "-background", "black",
            "-extent", f"{panel_w}x{panel_h}",
            str(dest),
        ],
        check=True,
    )


def render_panel_natural(photo: Path, panel_h: int, dest: Path) -> None:
    """Scale the photo to the panel height with no cropping at all --
    used for stacked panels, which show the full original composition."""
    subprocess.run(
        ["magick", str(photo), "-auto-orient", "-resize", f"x{panel_h}", str(dest)],
        check=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "photos", nargs="*", type=Path,
        help="render exactly these photo(s) as panels instead of picking "
             "randomly, e.g. to test stacking",
    )
    parser.add_argument(
        "--gap", type=int, default=None, metavar="PX",
        help="force this gutter size in px instead of the auto-computed "
             "leftover-space gap, for testing an explicit set of --photos.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    check_requirements(config.get("screen_size") is None)
    photo_roots = config["photo_roots"]

    screen_w, screen_h = screen_size(config)
    plugin = enabled_plugin(config, CONFIG_FILE)

    if args.photos:
        group = args.photos
        resolved = [p.resolve() for p in group]
        photo_base = Path(os.path.commonpath(resolved)) if len(resolved) > 1 else resolved[0].parent
    else:
        if not photo_roots:
            sys.exit(
                f"{CONFIG_FILE} has no 'photo_roots' set yet -- add at least "
                f"one folder to pick photos from and run this again."
            )
        photo_base = Path(os.path.commonpath(photo_roots))
        try:
            cache = load_cache()
        except RuntimeError as e:
            sys.exit(str(e))
        if not cache:
            sys.exit(
                "photo cache is empty -- random-wallpaper-scan.timer will "
                "build it, or run scan.py yourself."
            )
        picks = pick_group(cache, screen_w, screen_h)
        group = [p for p, _ in picks]
        cached_widths = {str(p): w for p, w in picks}

    n = len(group)

    with tempfile.TemporaryDirectory() as tmp:
        if n == 1:
            dest = Path(tmp) / "panel.jpg"
            render_panel(group[0], screen_w, screen_h, dest, plugin)
            shutil.move(dest, OUT)
        else:
            if args.photos:
                widths = [natural_width(p, screen_h) for p in group]
            else:
                widths = [cached_widths[str(p)] for p in group]
            if sum(widths) > screen_w:  # only possible with manually-picked --photos
                scale = screen_w / sum(widths)
                widths = [max(1, round(w * scale)) for w in widths]
            gap = args.gap if args.gap is not None else max(0, (screen_w - sum(widths)) // (n + 1))

            panels = []
            for i, photo in enumerate(group):
                dest = Path(tmp) / f"panel{i}.jpg"
                render_panel_natural(photo, screen_h, dest)
                panels.append(dest)

            x = gap
            cmd = ["magick", "-size", f"{screen_w}x{screen_h}", "xc:black"]
            for dest, width in zip(panels, widths):
                cmd += [str(dest), "-gravity", "northwest", "-geometry", f"+{x}+0", "-composite"]
                x += width + gap
            cmd += [str(OUT)]
            subprocess.run(cmd, check=True)

    uri = f"file://{OUT}"
    gsettings_targets = [
        ("org.gnome.desktop.background", ("picture-uri", "picture-uri-dark")),
        ("org.gnome.desktop.screensaver", ("picture-uri",)),
    ]
    for schema, keys in gsettings_targets:
        for key in keys:
            subprocess.run(["gsettings", "set", schema, key, uri], check=True)
        subprocess.run(["gsettings", "set", schema, "picture-options", "zoom"], check=True)

    labels = [relative_label(p, photo_base) for p in group]
    dates = dates_taken(group)
    SOURCE_FILE.write_text("\n".join(f"{l}\t{d}" for l, d in zip(labels, dates)) + "\n")
    print(f"wallpaper set from {', '.join(str(p) for p in group)}")


if __name__ == "__main__":
    main()
