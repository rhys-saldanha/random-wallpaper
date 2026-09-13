#!/usr/bin/env python3
"""Optional cropping plugins for random-wallpaper.py, enabled per-plugin in
~/.config/random-wallpaper/config.json under "plugins". Each plugin
declares the CLI tools it needs; enabled_plugin() checks those are on PATH
before ever using the plugin, and fails loudly (stderr + a desktop
notification) if a plugin is enabled but its tools aren't installed,
rather than silently degrading.

Currently just one plugin: "vision", which asks an opencode vision model
for the main subject's bounding box so a single photo can be cropped to
fill the screen without cutting off the subject. Add another plugin by
subclassing Plugin and registering it in PLUGINS.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path


class Plugin:
    """Base class for an optional subject-detection plugin."""
    name: str = ""
    required_tools: list[str] = []

    def subject_bbox(self, photo: Path) -> tuple[float, float, float, float]:
        """Returns (left, top, right, bottom) as fractions of the image --
        the bounding box of the main subject to keep in frame when
        cropping."""
        raise NotImplementedError


class VisionPlugin(Plugin):
    """Asks an opencode vision model to locate the main subject."""
    name = "vision"
    required_tools = ["opencode"]
    model = "opencode/mimo-v2.5-free"
    fallback = (0.1, 0.1, 0.9, 0.9)
    prompt = (
        'Identify the single main subject in this photo (the thing the '
        'photographer intended to be the focus). Reply with ONLY a compact '
        'JSON object like {"left":0.12,"top":0.05,"right":0.88,"bottom":0.95} '
        '- the bounding box of that subject as fractions of image width/height, '
        'no markdown fences, no other text.'
    )

    def subject_bbox(self, photo: Path) -> tuple[float, float, float, float]:
        """Ask the vision model for the subject's bounding box; fall back to
        a centred default if the call fails or returns something unusable."""
        try:
            proc = subprocess.run(
                ["opencode", "run", self.prompt, "-m", self.model, "--format", "json", "-f", str(photo)],
                capture_output=True, text=True, timeout=90,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"vision call failed: {exc}", file=sys.stderr)
            return self.fallback

        text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "text":
                text = evt["part"]["text"]

        try:
            box = json.loads(text[text.index("{"): text.rindex("}") + 1])
            l, t, r, b = box["left"], box["top"], box["right"], box["bottom"]
            if 0 <= l < r <= 1 and 0 <= t < b <= 1:
                return l, t, r, b
        except (ValueError, KeyError, TypeError):
            pass
        print(f"could not parse subject bbox from: {text!r}", file=sys.stderr)
        return self.fallback


PLUGINS: dict[str, Plugin] = {"vision": VisionPlugin()}


def enabled_plugin(config: dict, config_file: Path):
    """Returns the first enabled plugin from config['plugins'] (or None if
    none are enabled), after checking its required CLI tools are actually
    on PATH -- exits with a clear error (stderr message + a critical
    desktop notification) if a plugin is enabled but its tools aren't
    installed, rather than silently falling back to unrelated behaviour."""
    enabled = config.get("plugins", {})
    for name, plugin in PLUGINS.items():
        if not enabled.get(name):
            continue
        missing = [t for t in plugin.required_tools if shutil.which(t) is None]
        if missing:
            message = (
                f"Plugin '{name}' is enabled in {config_file} but requires "
                f"{', '.join(missing)}, which isn't on PATH. Install it, or "
                f"disable the plugin in your config."
            )
            print(message, file=sys.stderr)
            try:  # best-effort -- notify-send itself is optional, not a hard requirement
                subprocess.run(
                    ["notify-send", "-u", "critical", "-a", "Wallpaper",
                     f"Random Wallpaper: '{name}' plugin unavailable", message],
                    check=False,
                )
            except OSError:
                pass
            sys.exit(message)
        return plugin
    return None