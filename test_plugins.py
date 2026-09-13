#!/usr/bin/env python3
"""Unit tests for plugins.py (the optional cropping-plugin architecture).

Every system boundary is mocked (subprocess calls to opencode / notify-send,
and shutil.which for the PATH check), so tests run standalone with no opencode
installed and no display server required.

Run with:
    python3 -m unittest test_plugins -v
"""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import plugins


class VisionPluginTests(unittest.TestCase):
    @patch("plugins.subprocess.run")
    def test_parses_valid_opencode_json_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout=(
            '{"type":"text","part":{"text":'
            '"{\\"left\\":0.12,\\"top\\":0.05,\\"right\\":0.88,\\"bottom\\":0.95}"}}\n'
        ))
        box = plugins.VisionPlugin().subject_bbox(Path("a.jpg"))
        self.assertEqual(box, (0.12, 0.05, 0.88, 0.95))

    @patch("plugins.subprocess.run", side_effect=subprocess.TimeoutExpired("opencode", 90))
    def test_falls_back_on_timeout(self, mock_run):
        self.assertEqual(
            plugins.VisionPlugin().subject_bbox(Path("a.jpg")),
            plugins.VisionPlugin().fallback,
        )

    @patch("plugins.subprocess.run")
    def test_falls_back_on_unparsable_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="not json at all\n")
        plugin = plugins.VisionPlugin()
        self.assertEqual(plugin.subject_bbox(Path("a.jpg")), plugin.fallback)


class EnabledPluginTests(unittest.TestCase):
    @patch("plugins.shutil.which", return_value=None)
    def test_returns_none_when_plugins_key_empty(self, mock_which):
        self.assertIsNone(plugins.enabled_plugin({"plugins": {}}, Path("/tmp/config.json")))

    def test_returns_none_when_plugins_key_missing(self):
        self.assertIsNone(plugins.enabled_plugin({}, Path("/tmp/config.json")))

    @patch("plugins.shutil.which", return_value="/usr/bin/opencode")
    def test_returns_vision_plugin_when_enabled_and_on_path(self, mock_which):
        plugin = plugins.enabled_plugin({"plugins": {"vision": True}}, Path("/tmp/config.json"))
        self.assertIsInstance(plugin, plugins.VisionPlugin)

    @patch("plugins.subprocess.run")
    @patch("plugins.shutil.which", return_value=None)
    def test_sys_exits_and_notifies_when_enabled_but_tool_missing(self, mock_which, mock_run):
        config_file = Path("/tmp/config.json")
        with self.assertRaises(SystemExit):
            plugins.enabled_plugin({"plugins": {"vision": True}}, config_file)
        notify_call = mock_run.call_args.args[0]
        self.assertEqual(notify_call[0], "notify-send")
        self.assertTrue(any("vision" in str(x) for x in notify_call))

    @patch("plugins.subprocess.run", side_effect=FileNotFoundError)
    @patch("plugins.shutil.which", return_value=None)
    def test_sys_exits_cleanly_even_if_notify_send_itself_is_missing(self, mock_which, mock_run):
        # notify-send is best-effort -- its absence shouldn't crash with an
        # unhandled OSError instead of the intended clean SystemExit
        with self.assertRaises(SystemExit):
            plugins.enabled_plugin({"plugins": {"vision": True}}, Path("/tmp/config.json"))


if __name__ == "__main__":
    unittest.main()