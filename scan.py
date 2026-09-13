#!/usr/bin/env python3
"""Build the photo-metadata cache that random-wallpaper.py reads.

Owns ~/.cache/random-wallpaper.db. Walks the configured photo_roots and
stores, per JPEG, the EXIF capture date (epoch) and oriented dimensions,
keyed by path, so the wallpaper script can pick and stack photos without
touching the NAS beyond rendering the few it actually shows.

Extraction is one exiftool call per chunk that returns DateTimeOriginal,
ImageSize and Orientation in a single header pass per file -- half the file
opens (and half the subprocess spawns) of separate exiftool + identify
passes; dimensions are corrected for EXIF orientation locally. If exiftool
is missing or dies, capture dates fall back to file mtime and dimensions to
a per-photo identify call, so the cache still builds without it.

Incremental: a scan re-reads only files newer than the newest cached photo.
When the number of files on disk disagrees with the cache (a restored file
with an old mtime, or deletions), a complete sweep -- the per-file mtime
diff -- is authoritative, and rows for files no longer in the library are
pruned. Interrupt-safe: each chunk commits and prints progress, so a
timed-out run resumes where it left off next time.

Run on a schedule by random-wallpaper-scan.timer, or manually:
    scan.py
"""
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "random-wallpaper" / "config.json"
CACHE_DB = Path.home() / ".cache" / "random-wallpaper.db"
IMAGE_GLOBS = ("*.[Jj][Pp][Gg]", "*.[Jj][Pp][Ee][Gg]")
DEFAULT_CONFIG = {
    "photo_roots": [],
    "plugins": {"vision": False},
}
SCAN_CHUNK = 200  # photos per batch: one exiftool call each, keeps the db resumable
SCAN_PARALLEL = 4  # chunks scanned concurrently so NAS latency overlaps


def load_config() -> dict:
    """Read the shared config, creating a template alongside it if missing."""
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE) as f:
        config = {**DEFAULT_CONFIG, **json.load(f)}
    return config


def all_photos(photo_roots: list[str]) -> list[tuple[Path, float]]:
    """(path, mtime) for every image (see IMAGE_GLOBS) under photo_roots,
    searched recursively at any depth -- so photo_roots can just be one
    top-level library folder containing many camera/trip subfolders, no
    need to list every subfolder individually. mtime is just an
    invalidation key for the cache; the real capture date comes from EXIF."""
    photos = []
    for root in photo_roots:
        for pattern in IMAGE_GLOBS:
            for p in Path(root).rglob(pattern):
                try:
                    photos.append((p, p.stat().st_mtime))
                except OSError:
                    continue
    if not photos:
        raise RuntimeError(f"no photos found under {photo_roots} (mounted? configured correctly?)")
    return photos


def image_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["identify", "-auto-orient", "-format", "%w %h", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    w, h = map(int, out.split())
    return w, h


def _read_metadata(paths: list[tuple[Path, float]]) -> tuple[list[float], list[tuple[int, int]]]:
    """(capture epoch, oriented (w, h)) per (path, mtime), in order, from a
    single exiftool call that reads DateTimeOriginal, ImageSize and
    Orientation in one header pass per file. A per-line failure falls back to
    file mtime + a one-off identify call; if exiftool itself is missing or
    dies, every file degrades the same way, so a scan still completes."""
    lines: list[str] = []
    try:
        lines = subprocess.run(
            ["exiftool", "-n", "-T", "-DateTimeOriginal", "-ImageSize", "-Orientation",
             *[str(p) for p, _ in paths]],
            capture_output=True, text=True, timeout=90,
        ).stdout.splitlines()
    except (subprocess.TimeoutExpired, OSError):
        pass

    epochs: list[float] = []
    sizes: list[tuple[int, int]] = []
    for (p, mtime), line in zip(paths, lines):
        try:
            date_s, size_s, orient_s = line.split("\t")
            if date_s and date_s != "-":
                try:
                    epoch = float(date_s)
                except ValueError:
                    epoch = datetime.strptime(date_s, "%Y:%m:%d %H:%M:%S").timestamp()
            else:
                epoch = mtime
            w_s, h_s = size_s.split("x", 1)
            w, h = int(w_s), int(h_s)
            if orient_s.isdigit() and int(orient_s) >= 5:
                w, h = h, w
        except (ValueError, IndexError):
            epoch, (w, h) = mtime, image_size(p)
        epochs.append(epoch)
        sizes.append((w, h))

    # exiftool died part-way: fewer lines than files -- degrade the tail
    for p, mtime in paths[len(epochs):]:
        epochs.append(mtime)
        sizes.append(image_size(p))
    return epochs, sizes


def _scan_chunk(chunk: list[tuple[Path, float]]) -> list[tuple[str, float, float, int, int]]:
    """(path, mtime, taken, w, h) rows for one chunk."""
    epochs, sizes = _read_metadata(chunk)
    return [(str(p), m, epoch, w, h)
            for (p, m), epoch, (w, h) in zip(chunk, epochs, sizes)]


def _scan_chunks(changed: list[tuple[Path, float]]):
    """Yield each chunk's rows as they finish, scanning up to SCAN_PARALLEL
    chunks concurrently (an exiftool subprocess each, so NAS latency
    overlaps between them)."""
    submitted = 0
    with ThreadPoolExecutor(max_workers=SCAN_PARALLEL) as pool:
        pending: set = set()

        def fill() -> None:
            nonlocal submitted
            while submitted < len(changed) and len(pending) < SCAN_PARALLEL:
                chunk = changed[submitted:submitted + SCAN_CHUNK]
                pending.add(pool.submit(_scan_chunk, chunk))
                submitted += SCAN_CHUNK

        fill()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                yield fut.result()
                pending.discard(fut)
            fill()


def _known_mtimes(conn) -> dict[str, float]:
    return {row[0]: row[1] for row in conn.execute("SELECT path, mtime FROM photos")}


def _prune_stale(conn, current: set[str]) -> None:
    """Drop rows for photos no longer on disk (runs after a full sweep)."""
    conn.execute("CREATE TEMP TABLE cur (path TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO cur (path) VALUES (?)", ((p,) for p in current))
    conn.execute("DELETE FROM photos WHERE path NOT IN (SELECT path FROM cur)")
    conn.execute("DROP TABLE cur")
    conn.commit()


def build_cache(conn, photo_roots: list[str]) -> int:
    """Scan photo metadata into the cache. Incremental: only files newer than
    the newest cached photo are re-read; if the file count on disk disagrees
    with the cache (a new file restored with an old mtime, or deletions), a
    complete sweep -- the per-file mtime diff -- is authoritative, and stale
    rows are pruned. Returns how many photos were scanned."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS photos "
        "(path TEXT PRIMARY KEY, mtime REAL, taken REAL, w INT, h INT)"
    )
    photos = all_photos(photo_roots)
    known = _known_mtimes(conn)
    if len(known) == len(photos):
        newest = max(known.values()) if known else None
        changed = [(p, m) for p, m in photos if newest is None or m > newest]
    else:
        changed = [(p, m) for p, m in photos if known.get(str(p)) != m]

    scanned = 0
    total = len(changed)
    for rows in _scan_chunks(changed):
        conn.executemany(
            "INSERT OR REPLACE INTO photos (path, mtime, taken, w, h) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        scanned += len(rows)
        print(f"scanned {scanned}/{total} photos for cache", flush=True)

    if len(known) != len(photos):
        _prune_stale(conn, {str(p) for p, _ in photos})
    return scanned


def main() -> None:
    config = load_config()
    photo_roots = config["photo_roots"]
    if not photo_roots:
        sys.exit(
            f"{CONFIG_FILE} has no 'photo_roots' set yet -- add at least "
            f"one folder to scan and run this again."
        )
    conn = sqlite3.connect(CACHE_DB)
    try:
        scanned = build_cache(conn, photo_roots)
    finally:
        conn.close()
    if scanned:
        print(f"cache updated ({scanned} photos scanned) in {CACHE_DB}")
    else:
        print(f"cache up to date in {CACHE_DB}")


if __name__ == "__main__":
    main()