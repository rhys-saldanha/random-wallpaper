#!/usr/bin/env python3
"""Tests for scan.py -- the scheduled cache builder (EXIF capture dates +
dimensions into ~/.cache/random-wallpaper.db, read by random-wallpaper.py).

Every system boundary is mocked: the NAS walk, exiftool, and identify are
all fakes. Runs standalone -- no display, no NAS mount, no image tools.

Run with:
    python3 -m unittest test_scan -v
"""
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).parent
if "scan" not in sys.modules:
    spec = importlib.util.spec_from_file_location("scan", SCRIPT_DIR / "scan.py")
    scan = importlib.util.module_from_spec(spec)
    sys.modules["scan"] = scan
    spec.loader.exec_module(scan)
else:
    scan = sys.modules["scan"]

GOOD_EXIF = "1771297916\t4000x3000\t1"  # epoch capture, dims, upright orientation


def fake_run_good(cmd, **kwargs):
    name = Path(cmd[0]).name
    if name == "exiftool":
        n = len(cmd) - 6  # -n -T -DateTimeOriginal -ImageSize -Orientation files...
        return MagicMock(stdout=(GOOD_EXIF + "\n") * n)
    if name == "identify":
        n = len(cmd) - 4  # -auto-orient -format "%w %h" file...
        return MagicMock(stdout="4000 3000\n" * n)
    raise AssertionError(f"unexpected command in test: {cmd}")


class CacheTests(unittest.TestCase):
    """build_cache(): the sqlite photo-metadata cache scan.py owns."""

    def _cache(self, root, db, fake=None):
        with patch("scan.subprocess.run", side_effect=fake or fake_run_good):
            with sqlite3.connect(db) as conn:
                scan.build_cache(conn, [str(root)])
                rows = {r[0]: (r[2], r[3], r[4])
                        for r in conn.execute("SELECT path, mtime, taken, w, h FROM photos")}
        return rows

    @staticmethod
    def _make_roots(tmp):
        root = Path(tmp) / "Camera"
        a, b = root / "a.JPG", root / "sub" / "b.JPG"
        b.parent.mkdir(parents=True)
        for p in (a, b):
            p.write_bytes(b"x")
        return root, a, b

    def test_first_run_reads_exif_and_dims_into_db(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, b = self._make_roots(tmp)
            seen = []
            def fake(cmd, **kw):
                seen.append(Path(cmd[0]).name)
                return fake_run_good(cmd, **kw)
            rows = self._cache(root, Path(dbdir) / "cache.db", fake)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[str(a)][0], 1771297916.0)
        self.assertEqual(rows[str(a)][1:], (4000, 3000))
        self.assertEqual(seen.count("exiftool"), 1)  # one call per chunk, not per file
        self.assertNotIn("identify", seen)  # dims come from the same exiftool pass

    def test_old_style_date_string_is_parsed_too(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, _ = self._make_roots(tmp)
            def fake(cmd, **kw):
                if Path(cmd[0]).name == "exiftool":
                    n = len(cmd) - 6
                    return MagicMock(stdout="2026:03:18 10:58:36\t4000x3000\t1\n" * n)
                return fake_run_good(cmd, **kw)
            rows = self._cache(root, Path(dbdir) / "cache.db", fake)
        self.assertEqual(rows[str(a)][0], datetime(2026, 3, 18, 10, 58, 36).timestamp())

    def test_exif_orientation_swaps_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, _ = self._make_roots(tmp)
            def fake(cmd, **kw):
                if Path(cmd[0]).name == "exiftool":
                    n = len(cmd) - 6
                    return MagicMock(stdout="1771297916\t4000x3000\t6\n" * n)
                return fake_run_good(cmd, **kw)
            rows = self._cache(root, Path(dbdir) / "cache.db", fake)
        self.assertEqual(rows[str(a)][1:], (3000, 4000))  # rotated 90°, like -auto-orient

    def test_unchanged_files_are_reused_without_external_calls(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, _, _ = self._make_roots(tmp)
            db = Path(dbdir) / "cache.db"
            self._cache(root, db)  # first build
            with patch("scan.subprocess.run") as mock_run:
                rows = self._cache(root, db, lambda cmd, **kw: mock_run(cmd, **kw))
                mock_run.assert_not_called()
        self.assertEqual(len(rows), 2)

    def test_changed_mtime_triggers_reread(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, _ = self._make_roots(tmp)
            db = Path(dbdir) / "cache.db"
            self._cache(root, db)
            new_mtime = a.stat().st_mtime + 5000
            import os
            os.utime(a, (new_mtime, new_mtime))  # bump mtime without touching contents
            seen = []
            def fake(cmd, **kw):
                seen.append(Path(cmd[0]).name)
                return fake_run_good(cmd, **kw)
            rows = self._cache(root, db, fake)
        # only the changed file was re-read, in one exiftool call, no identify
        self.assertEqual(len(rows), 2)
        self.assertEqual(seen, ["exiftool"])

    def test_older_mtime_addition_is_caught_by_count_mismatch(self):
        # the lazy incremental path only looks at files newer than the newest
        # cached photo, so a file restored with an old mtime would be missed --
        # the count guard falls back to a full sweep (the complete diff) and
        # finds it anyway.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, _ = self._make_roots(tmp)
            db = Path(dbdir) / "cache.db"
            self._cache(root, db)
            c = root / "c.JPG"
            c.write_bytes(b"x")
            old = a.stat().st_mtime  # older than b (the newest file), so the
            import os
            os.utime(c, (old, old))  # threshold alone would not pick it up
            seen = []
            def fake(cmd, **kw):
                seen.append(Path(cmd[0]).name)
                return fake_run_good(cmd, **kw)
            rows = self._cache(root, db, fake)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[str(c)][1:], (4000, 3000))
        self.assertEqual(seen, ["exiftool"])

    def test_deleted_photo_is_pruned_by_full_sweep(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, b = self._make_roots(tmp)
            db = Path(dbdir) / "cache.db"
            self._cache(root, db)
            a.unlink()
            with patch("scan.subprocess.run") as mock_run:
                rows = self._cache(root, db, lambda cmd, **kw: mock_run(cmd, **kw))
                mock_run.assert_not_called()  # nothing new to read, just the prune
        self.assertEqual(set(rows), {str(b)})

    def test_chunked_scan_is_resumable_across_an_interrupt(self):
        # > SCAN_CHUNK files so several chunks run; let exiftool die on the
        # second chunk (simulating a timeout mid-scan). The first chunk must
        # already be committed, and a re-run must finish the rest.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root = Path(tmp) / "Camera"
            root.mkdir()
            for i in range(scan.SCAN_CHUNK * 2 + 1):
                (root / f"p{i:04}.jpg").write_bytes(b"x")
            db = Path(dbdir) / "cache.db"

            calls = {"chunks": 0}
            def fake(cmd, **kw):
                if Path(cmd[0]).name == "exiftool":
                    calls["chunks"] += 1
                    if calls["chunks"] == 2:
                        raise subprocess.SubprocessError("simulated interrupt")
                return fake_run_good(cmd, **kw)

            with patch("scan.SCAN_PARALLEL", 1):  # serial chunks -> deterministic order
                with patch("scan.subprocess.run", side_effect=fake):
                    with sqlite3.connect(db) as conn:
                        with self.assertRaises(subprocess.SubprocessError):
                            scan.build_cache(conn, [str(root)])
            with sqlite3.connect(db) as conn:
                first = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            # first chunk (SCAN_CHUNK photos) was committed before the crash
            self.assertEqual(first, scan.SCAN_CHUNK)

            # re-run completes the remaining files; already-scanned ones aren't re-read
            with patch("scan.SCAN_PARALLEL", 1):
                rows = self._cache(root, db)
            self.assertEqual(len(rows), scan.SCAN_CHUNK * 2 + 1)

    def test_exiftool_missing_degrades_to_mtime_dates_and_identify_dims(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dbdir:
            root, a, _ = self._make_roots(tmp)
            mtime = a.stat().st_mtime
            def fake(cmd, **kw):
                if Path(cmd[0]).name == "exiftool":
                    raise OSError("no exiftool")
                return fake_run_good(cmd, **kw)
            rows = self._cache(root, Path(dbdir) / "cache.db", fake)
        self.assertEqual(rows[str(a)][0], mtime)
        self.assertEqual(rows[str(a)][1:], (4000, 3000))


class AllPhotosTests(unittest.TestCase):
    def test_walks_configured_roots_and_returns_mtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Camera"
            (root / "sub").mkdir(parents=True)
            (root / "a.JPG").write_bytes(b"x")
            (root / "sub" / "b.jpg").write_bytes(b"x")
            (root / "ignored.txt").write_bytes(b"x")
            photos = scan.all_photos([str(root)])
        self.assertEqual(sorted(p.name for p, _ in photos), ["a.JPG", "b.jpg"])

    def test_finds_everything_at_any_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "Library"
            deep = library / "CameraA" / "2024" / "Trip" / "RawJpgs"
            deep.mkdir(parents=True)
            (deep / "a.jpg").write_bytes(b"x")
            (library / "CameraB" / "b.JPG").parent.mkdir(parents=True)
            (library / "CameraB" / "b.JPG").write_bytes(b"x")
            photos = scan.all_photos([str(library)])
        self.assertEqual(sorted(p.name for p, _ in photos), ["a.jpg", "b.JPG"])

    def test_raises_if_no_photos_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                scan.all_photos([tmp])


class LoadConfigTests(unittest.TestCase):
    def test_creates_template_config_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            with patch("scan.CONFIG_FILE", missing):
                config = scan.load_config()
            self.assertEqual(config, dict(scan.DEFAULT_CONFIG))
            self.assertTrue(missing.exists())
            self.assertFalse(json.loads(missing.read_text())["plugins"]["vision"])

    def test_loads_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text('{"photo_roots": ["/a", "/b"]}')
            with patch("scan.CONFIG_FILE", cfg):
                config = scan.load_config()
        self.assertEqual(config["photo_roots"], ["/a", "/b"])
        self.assertIn("plugins", config)  # merged in from DEFAULT_CONFIG

    def test_tolerates_extra_keys_like_the_shipped_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text('{"_note": "explanatory text", "photo_roots": ["/a"]}')
            with patch("scan.CONFIG_FILE", cfg):
                config = scan.load_config()
        self.assertEqual(config["photo_roots"], ["/a"])


class ScanMainTests(unittest.TestCase):
    def test_main_builds_cache_without_touching_wallpaper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "cache.db"
            root = tmp / "Camera"
            root.mkdir()
            (root / "a.jpg").write_bytes(b"x")
            config_file = tmp / "config.json"
            config_file.write_text(json.dumps({"photo_roots": [str(root)]}))

            with patch("scan.CONFIG_FILE", config_file), \
                 patch("scan.CACHE_DB", db), \
                 patch("scan.subprocess.run", side_effect=fake_run_good) as run:
                scan.main()

            commands = [Path(c.args[0][0]).name for c in run.call_args_list]
            self.assertIn("exiftool", commands)
            # no display, wallpaper, or compose commands -- scan only, in one pass
            self.assertNotIn("xrandr", commands)
            self.assertNotIn("gsettings", commands)
            self.assertNotIn("magick", commands)
            self.assertNotIn("identify", commands)

            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 1)

    def test_no_photo_roots_exits_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text('{"photo_roots": []}')
            with patch("scan.CONFIG_FILE", cfg):
                with self.assertRaises(SystemExit) as ctx:
                    scan.main()
            self.assertIn("photo_roots", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()