#!/usr/bin/env python3
"""Unit + integration tests for random-wallpaper.py -- the wallpaper half
(pick, crop, stack, apply). Cache building moved to scan.py and covered by
test_scan.py; here load_cache() reads whatever test_scan.py would have built.

Every system boundary is mocked: subprocess calls to magick/identify/
exiftool/xrandr/gsettings, the vision plugin's opencode call (see
plugins.py), and the config file / NAS photo-library location (photo_roots).
Tests run standalone -- no display, no NAS mount, no GNOME session, no real
image tools required.

Run with:
    python3 -m unittest test_random_wallpaper -v
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("random_wallpaper", SCRIPT_DIR / "random-wallpaper.py")
rw = importlib.util.module_from_spec(SPEC)
sys.modules["random_wallpaper"] = rw
SPEC.loader.exec_module(rw)


class ComputeCropTests(unittest.TestCase):
    def test_narrow_photo_crops_height_to_target_ratio(self):
        # small, centred subject -- the target ratio should win outright,
        # not the subject-margin floor
        x0, y0, w, h = rw.compute_crop(4000, 3000, (0.4, 0.4, 0.6, 0.6), target_ratio=16 / 9)
        self.assertEqual((x0, w), (0, 4000))
        self.assertAlmostEqual(w / h, 16 / 9, places=2)

    def test_wide_photo_crops_width_to_target_ratio(self):
        x0, y0, w, h = rw.compute_crop(6000, 1000, (0.45, 0.3, 0.55, 0.7), target_ratio=4 / 3)
        self.assertEqual((y0, h), (0, 1000))
        self.assertAlmostEqual(w / h, 4 / 3, places=2)

    def test_large_subject_backs_off_target_ratio_to_preserve_margin(self):
        # subject fills most of the frame -- the crop should favour the
        # subject-margin floor over hitting the target ratio exactly
        x0, y0, w, h = rw.compute_crop(4000, 3000, (0.1, 0.1, 0.9, 0.9), target_ratio=16 / 9)
        self.assertLess(w / h, 16 / 9)
        self.assertGreaterEqual(h, (0.9 - 0.1) * 3000 * rw.SUBJECT_MARGIN - 1)

    def test_never_cuts_into_the_subject_box(self):
        x0, y0, w, h = rw.compute_crop(4000, 3000, (0.05, 0.05, 0.95, 0.95), target_ratio=3.56)
        sub_left, sub_top = 0.05 * 4000, 0.05 * 3000
        sub_right, sub_bottom = 0.95 * 4000, 0.95 * 3000
        self.assertLessEqual(x0, sub_left)
        self.assertGreaterEqual(x0 + w, sub_right)
        self.assertLessEqual(y0, sub_top)
        self.assertGreaterEqual(y0 + h, sub_bottom)

    def test_offcentre_subject_is_kept_in_frame(self):
        x0, y0, w, h = rw.compute_crop(4000, 3000, (0.7, 0.1, 0.95, 0.4), target_ratio=1.0)
        self.assertLessEqual(x0, 0.7 * 4000)
        self.assertGreaterEqual(x0 + w, 0.95 * 4000)


class RelativeLabelTests(unittest.TestCase):
    def test_strips_photo_base_prefix(self):
        base = Path("/photos")
        label = rw.relative_label(base / "Camera" / "trip" / "a.JPG", base)
        self.assertEqual(label, "Camera/trip/a.JPG")

    def test_falls_back_to_full_path_outside_base(self):
        outside = Path("/tmp/not-under-photo-base/a.JPG")
        self.assertEqual(rw.relative_label(outside, Path("/photos")), str(outside))


class NaturalWidthTests(unittest.TestCase):
    @patch("random_wallpaper.subprocess.run")
    def test_scales_by_aspect_ratio(self, mock_run):
        mock_run.return_value = MagicMock(stdout="4000 3000\n")
        self.assertEqual(rw.natural_width(Path("photo.jpg"), screen_h=1440), round(4000 / 3000 * 1440))


class PickGroupTests(unittest.TestCase):
    """Exercises the greedy fill-until-full algorithm against a fake cache of
    (capture epoch, w, h), so it doesn't need real image files or EXIF reads.

    Dims are in screen-height-scaled-width units: cache height is always 1440
    (matching the test screen_h), so w/h*1440 == w straight away."""

    SCREEN_H = 1440

    @classmethod
    def cache(cls, entries, taken=0.0):
        return {name: (taken, w, cls.SCREEN_H) for name, w in entries}

    def group_paths(self, cache, **kwargs):
        picks = rw.pick_group(cache, screen_w=kwargs.pop("screen_w", 5120), screen_h=self.SCREEN_H, **kwargs)
        return [p for p, _ in picks]

    def test_stops_once_nothing_else_fits(self):
        # anchor + one 1920-wide companion = 3840/5120 = 75% full; a third
        # same-width candidate would overflow, so it should stop at 2
        cache = self.cache([("anchor.jpg", 1920), ("b.jpg", 1920), ("c.jpg", 1920)])
        with patch("random_wallpaper.random.choice", return_value=("anchor.jpg", cache["anchor.jpg"])):
            group = self.group_paths(cache)
        self.assertEqual(len(group), 2)

    def test_ignores_companions_outside_the_date_window(self):
        cache = {
            "anchor.jpg": (0.0, 1000, self.SCREEN_H),
            "far.jpg": (rw.NEARBY_SECONDS * 10, 1000, self.SCREEN_H),
        }
        with patch("random_wallpaper.random.choice", return_value=("anchor.jpg", cache["anchor.jpg"])):
            group = self.group_paths(cache)
        self.assertEqual(group, [Path("anchor.jpg")])

    def test_groups_photos_with_matching_exif_capture_dates(self):
        # Times are EXIF capture dates (absent = fallback to mtime): two
        # photos snapped days apart by file mtime but on the same day per
        # EXIF are companions. Here "taken" is the EXIF epoch and mtimes are
        # wildly different, they differ in the "name" field only.
        cache = {
            "anchor.jpg": (1000.0, 1000, self.SCREEN_H),
            "b.jpg": (1000.0, 1000, self.SCREEN_H),  # same EXIF day, different file
        }
        with patch("random_wallpaper.random.choice", return_value=("anchor.jpg", cache["anchor.jpg"])):
            picks = self.group_paths(cache)
        self.assertEqual(picks, [Path("anchor.jpg"), Path("b.jpg")])

    def test_keeps_adding_narrow_photos_while_they_fit(self):
        # A run of narrow same-date photos keeps being added until the
        # screen or FILL_RATIO is reached -- the width, not a panel cap,
        # bounds how many fit.
        widths = {f"p{i}.jpg": 400 for i in range(10)}
        cache = {name: (float(i), w, self.SCREEN_H) for i, (name, w) in enumerate(widths.items())}
        with patch("random_wallpaper.random.choice", return_value=("p0.jpg", cache["p0.jpg"])):
            group = self.group_paths(cache)
        self.assertEqual(len(group), 10)  # 400*10 = 4000 <= 5120, below FILL_RATIO

    def test_swaps_out_a_narrow_panel_when_a_wider_photo_would_fill(self):
        # When a wide photo is shuffled late and doesn't fit the leftover
        # width, it still gets in by replacing the narrowest added panel if
        # that fills more without overflowing -- no avoidable gap.
        cache = {
            "anchor.jpg": (0.0, 1500, self.SCREEN_H),
            "small.jpg": (0.0, 1200, self.SCREEN_H),
            "wide.jpg": (0.0, 3200, self.SCREEN_H),
        }
        with patch("random_wallpaper.random.choice", return_value=("anchor.jpg", cache["anchor.jpg"])), \
             patch("random_wallpaper.random.shuffle", side_effect=lambda _: None):
            picks = rw.pick_group(cache, screen_w=5120, screen_h=self.SCREEN_H)
        self.assertEqual([p for p, _ in picks], [Path("anchor.jpg"), Path("wide.jpg")])

    def test_stops_once_fill_ratio_reached(self):
        cache = self.cache([("anchor.jpg", 4700), ("b.jpg", 100)])
        with patch("random_wallpaper.random.choice", return_value=("anchor.jpg", cache["anchor.jpg"])):
            group = self.group_paths(cache)
        # anchor alone is already >= FILL_RATIO of the screen width
        self.assertEqual(group, [Path("anchor.jpg")])

    def test_returns_path_width_pairs_for_the_compose_step(self):
        cache = self.cache([("anchor.jpg", 2000), ("b.jpg", 1000)])
        with patch("random_wallpaper.random.choice", return_value=("anchor.jpg", cache["anchor.jpg"])):
            picks = rw.pick_group(cache, screen_w=5120, screen_h=self.SCREEN_H)
        self.assertEqual(picks, [(Path("anchor.jpg"), 2000), (Path("b.jpg"), 1000)])


class ScreenSizeTests(unittest.TestCase):
    @patch("random_wallpaper.subprocess.run")
    def test_parses_connected_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout=(
            "Screen 0: minimum 320 x 200\n"
            "DP-0 connected primary 5120x1440+0+0 ...\n"
            "HDMI-1 disconnected\n"
        ))
        self.assertEqual(rw.screen_size(), (5120, 1440))

    @patch("random_wallpaper.subprocess.run")
    def test_raises_if_nothing_connected(self, mock_run):
        mock_run.return_value = MagicMock(stdout="Screen 0: minimum 320 x 200\nHDMI-1 disconnected\n")
        with self.assertRaises(RuntimeError):
            rw.screen_size()


class CheckRequirementsTests(unittest.TestCase):
    @patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake")
    def test_passes_silently_when_everything_is_on_path(self, mock_which):
        rw.check_requirements()  # must not raise

    @patch("random_wallpaper.shutil.which")
    def test_exits_listing_exactly_whats_missing(self, mock_which):
        mock_which.side_effect = lambda tool: None if tool in ("magick", "gsettings") else f"/usr/bin/{tool}"
        with self.assertRaises(SystemExit) as ctx:
            rw.check_requirements()
        message = str(ctx.exception)
        self.assertIn("magick", message)
        self.assertIn("gsettings", message)
        self.assertNotIn("xrandr", message)
        self.assertNotIn("identify", message)


class DatesTakenTests(unittest.TestCase):
    @patch("random_wallpaper.subprocess.run")
    def test_formats_exif_date(self, mock_run):
        mock_run.return_value = MagicMock(stdout="2026:03:18 10:58:36\n")
        self.assertEqual(rw.dates_taken([Path("a.jpg")]), ["18 Mar 2026"])

    @patch("random_wallpaper.Path.stat")
    @patch("random_wallpaper.subprocess.run")
    def test_falls_back_to_mtime_when_tag_is_blank(self, mock_run, mock_stat):
        mock_run.return_value = MagicMock(stdout="-\n")  # exiftool prints '-' for a missing tag
        mock_stat.return_value = MagicMock(st_mtime=datetime(2020, 1, 2).timestamp())
        self.assertEqual(rw.dates_taken([Path("a.jpg")]), ["02 Jan 2020"])

    @patch("random_wallpaper.Path.stat")
    @patch("random_wallpaper.subprocess.run", side_effect=FileNotFoundError)
    def test_falls_back_to_mtime_when_exiftool_missing(self, mock_run, mock_stat):
        mock_stat.return_value = MagicMock(st_mtime=datetime(2020, 1, 2).timestamp())
        self.assertEqual(rw.dates_taken([Path("a.jpg")]), ["02 Jan 2020"])


class RenderPanelCommandTests(unittest.TestCase):
    """Checks the exact magick invocations, without needing magick installed."""

    @patch("random_wallpaper.image_size", return_value=(4000, 3000))
    @patch("random_wallpaper.subprocess.run")
    def test_full_bleed_panel_crops_then_resizes_to_exact_slot(self, mock_run, mock_size):
        plugin = MagicMock()
        plugin.subject_bbox.return_value = (0.1, 0.1, 0.9, 0.9)
        rw.render_panel(Path("a.jpg"), panel_w=1000, panel_h=1440, dest=Path("/tmp/out.jpg"), plugin=plugin)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "magick")
        self.assertIn("-crop", cmd)
        self.assertIn("-resize", cmd)
        self.assertIn("1000x1440", cmd)
        self.assertIn("-extent", cmd)
        plugin.subject_bbox.assert_called_once_with(Path("a.jpg"))

    @patch("random_wallpaper.image_size", return_value=(4000, 3000))
    @patch("random_wallpaper.subprocess.run")
    def test_no_plugin_falls_back_to_centred_box(self, mock_run, mock_size):
        rw.render_panel(Path("a.jpg"), panel_w=1000, panel_h=1440, dest=Path("/tmp/out.jpg"), plugin=None)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "magick")
        self.assertIn("-crop", cmd)
        self.assertIn("-resize", cmd)
        self.assertIn("-extent", cmd)

    @patch("random_wallpaper.subprocess.run")
    def test_natural_panel_only_constrains_height(self, mock_run):
        rw.render_panel_natural(Path("a.jpg"), panel_h=1440, dest=Path("/tmp/out.jpg"))
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "magick")
        self.assertIn("x1440", cmd)
        self.assertNotIn("-crop", cmd)
        self.assertNotIn("-extent", cmd)


class MainIntegrationTests(unittest.TestCase):
    """End-to-end main() with every system boundary mocked: no display, no
    NAS, no GNOME session, and no real image tools required."""

    @staticmethod
    def _fake_run(cmd, **kwargs):
        exe = Path(cmd[0]).name
        if exe == "xrandr":
            return MagicMock(stdout="DP-0 connected primary 5120x1440+0+0\n")
        if exe == "identify":
            n = len(cmd) - 4  # identify -auto-orient -format "%w %h" file...
            return MagicMock(stdout="4608 3456\n" * n)
        if exe == "exiftool":
            n = len(cmd) - 3  # exiftool -T -DateTimeOriginal file...
            return MagicMock(stdout="2026:01:01 12:00:00\n" * n)
        if exe == "opencode":
            return MagicMock(stdout=(
                '{"type":"text","part":{"text":'
                '"{\\"left\\":0.1,\\"top\\":0.1,\\"right\\":0.9,\\"bottom\\":0.9}"}}\n'
            ))
        if exe == "magick":
            Path(cmd[-1]).write_bytes(b"fake-image")  # so shutil.move has something real to move
            return MagicMock(returncode=0)
        if exe == "gsettings":
            return MagicMock(returncode=0)
        raise AssertionError(f"unexpected command in test: {cmd}")

    @staticmethod
    def _seed_cache(db, paths, taken):
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE photos (path TEXT PRIMARY KEY, mtime REAL, taken REAL, w INT, h INT)"
            )
            conn.executemany(
                "INSERT INTO photos (path, mtime, taken, w, h) VALUES (?, ?, ?, ?, ?)",
                [(str(p), 1.0, taken, 4608, 3456) for p in paths],
            )
            conn.commit()

    def test_two_photo_stack_writes_source_file_and_wallpaper_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, source = tmp / "out.jpg", tmp / "source.txt"
            config_file = tmp / "config.json"
            photos = [tmp / "a.jpg", tmp / "b.jpg"]
            for p in photos:
                p.write_bytes(b"x")

            with patch("random_wallpaper.OUT", out), \
                 patch("random_wallpaper.SOURCE_FILE", source), \
                 patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.subprocess.run", side_effect=self._fake_run) as run, \
                 patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake"), \
                 patch("sys.argv", ["random-wallpaper.py", str(photos[0]), str(photos[1])]):
                rw.main()

            lines = source.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertIn("\t", line)

            gsettings_cmds = [c.args[0] for c in run.call_args_list if Path(c.args[0][0]).name == "gsettings"]
            self.assertTrue(any("org.gnome.desktop.background" in c for c in gsettings_cmds))
            self.assertTrue(any("org.gnome.desktop.screensaver" in c for c in gsettings_cmds))

    def test_single_photo_fills_whole_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, source = tmp / "out.jpg", tmp / "source.txt"
            config_file = tmp / "config.json"
            photo = tmp / "a.jpg"
            photo.write_bytes(b"x")

            with patch("random_wallpaper.OUT", out), \
                 patch("random_wallpaper.SOURCE_FILE", source), \
                 patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.subprocess.run", side_effect=self._fake_run), \
                 patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake"), \
                 patch("sys.argv", ["random-wallpaper.py", str(photo)]):
                rw.main()

            self.assertTrue(out.exists())
            self.assertEqual(len(source.read_text().strip().splitlines()), 1)

    def test_no_photos_and_empty_photo_roots_exits_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text('{"photo_roots": []}')
            with patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake"), \
                 patch("sys.argv", ["random-wallpaper.py"]):
                with self.assertRaises(SystemExit):
                    rw.main()

    def test_vision_plugin_enabled_invokes_opencode_for_single_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, source = tmp / "out.jpg", tmp / "source.txt"
            config_file = tmp / "config.json"
            config_file.write_text('{"photo_roots": [], "plugins": {"vision": true}}')
            photo = tmp / "a.jpg"
            photo.write_bytes(b"x")

            with patch("random_wallpaper.OUT", out), \
                 patch("random_wallpaper.SOURCE_FILE", source), \
                 patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.subprocess.run", side_effect=self._fake_run) as run, \
                 patch("plugins.shutil.which", return_value="/usr/bin/opencode"), \
                 patch("sys.argv", ["random-wallpaper.py", str(photo)]):
                rw.main()

            self.assertTrue(out.exists())
            opencode_cmds = [c.args[0] for c in run.call_args_list if Path(c.args[0][0]).name == "opencode"]
            self.assertEqual(len(opencode_cmds), 1)
            self.assertEqual(opencode_cmds[0][0], "opencode")

    def test_random_mode_reads_prebuilt_cache_then_stacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, source = tmp / "out.jpg", tmp / "source.txt"
            config_file = tmp / "config.json"
            db = tmp / "cache.db"
            root = tmp / "Camera"
            (root / "sub").mkdir(parents=True)
            paths = [root / "sub" / "a.jpg", root / "sub" / "b.jpg"]
            for p in paths:
                p.write_bytes(b"x")
            config_file.write_text(json.dumps({"photo_roots": [str(root)]}))
            taken = datetime(2025, 12, 31).timestamp()  # same day -> one stack

            with patch("random_wallpaper.OUT", out), \
                 patch("random_wallpaper.SOURCE_FILE", source), \
                 patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.CACHE_DB", db), \
                 patch("random_wallpaper.subprocess.run", side_effect=self._fake_run), \
                 patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake"), \
                 patch("sys.argv", ["random-wallpaper.py"]):
                self._seed_cache(db, paths, taken)
                rw.main()

            self.assertTrue(out.exists())
            lines = source.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertIn("\t", line)
            # capture dates in the source file came out of the cache as "1 Jan 2026"
            self.assertIn("1 Jan 2026", lines[0])

    def test_missing_cache_exits_with_scan_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config_file = tmp / "config.json"
            root = tmp / "Camera"
            root.mkdir()
            config_file.write_text(json.dumps({"photo_roots": [str(root)]}))
            with patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.CACHE_DB", tmp / "nope.db"), \
                 patch("random_wallpaper.subprocess.run", side_effect=self._fake_run), \
                 patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake"), \
                 patch("sys.argv", ["random-wallpaper.py"]):
                with self.assertRaises(SystemExit) as ctx:
                    rw.main()
            self.assertIn("scan.py", str(ctx.exception))

    def test_empty_cache_exits_with_scan_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config_file = tmp / "config.json"
            db = tmp / "cache.db"
            root = tmp / "Camera"
            root.mkdir()
            config_file.write_text(json.dumps({"photo_roots": [str(root)]}))
            with sqlite3.connect(db) as conn:  # valid empty cache
                conn.execute(
                    "CREATE TABLE photos (path TEXT PRIMARY KEY, mtime REAL, taken REAL, w INT, h INT)"
                )
                conn.commit()
            with patch("scan.CONFIG_FILE", config_file), \
                 patch("random_wallpaper.CACHE_DB", db), \
                 patch("random_wallpaper.subprocess.run", side_effect=self._fake_run), \
                 patch("random_wallpaper.shutil.which", return_value="/usr/bin/fake"), \
                 patch("sys.argv", ["random-wallpaper.py"]):
                with self.assertRaises(SystemExit) as ctx:
                    rw.main()
            self.assertIn("empty", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()