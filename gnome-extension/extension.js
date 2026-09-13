import GObject from 'gi://GObject';
import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {parseSourceFile} from './parseSources.js';

const SOURCE_FILE = GLib.build_filenamev([GLib.get_home_dir(), '.cache', 'random-wallpaper-source.txt']);

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extensionPath) {
        super._init(0.0, 'Random Wallpaper', false);
        console.log('[random-wallpaper] indicator constructed');

        // random-wallpaper.py lives one directory above this extension
        // (the repo root), regardless of where the repo was cloned to.
        this._script = GLib.build_filenamev([extensionPath, '..', 'random-wallpaper.py']);

        const iconPath = GLib.build_filenamev([extensionPath, 'icons', 'wallpaper-symbolic.svg']);
        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(iconPath),
            style_class: 'system-status-icon',
        }));

        this.menu.connect('open-state-changed', (menu, open) => {
            console.log(`[random-wallpaper] open-state-changed: ${open}`);
            if (open)
                this._rebuild();
        });

        this._rebuild();
    }

    _readSources() {
        try {
            const [ok, contents] = GLib.file_get_contents(SOURCE_FILE);
            if (!ok)
                return [];
            return parseSourceFile(new TextDecoder().decode(contents));
        } catch (e) {
            return [];
        }
    }

    _rebuild() {
        console.log('[random-wallpaper] rebuilding menu');
        this.menu.removeAll();

        const photos = this._readSources();
        const header = new PopupMenu.PopupMenuItem(
            photos.length
                ? `Current wallpaper (${photos.length} photo${photos.length === 1 ? '' : 's'}):`
                : 'No wallpaper info yet');
        header.label.style = 'font-weight: bold;';
        this.menu.addMenuItem(header);

        for (const {path, date} of photos) {
            const item = new PopupMenu.PopupBaseMenuItem();
            const box = new St.BoxLayout({vertical: true, style: 'padding: 2px 0;'});
            box.add_child(new St.Label({text: path, style: 'font-size: 0.9em;'}));
            if (date)
                box.add_child(new St.Label({text: date, style: 'font-size: 0.8em;'}));
            item.add_child(box);
            this.menu.addMenuItem(item);
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const reroll = new PopupMenu.PopupMenuItem('Reroll wallpaper');
        reroll.connect('activate', () => {
            GLib.spawn_async(null, [this._script], null, GLib.SpawnFlags.SEARCH_PATH, null);
        });
        this.menu.addMenuItem(reroll);
    }
});

export default class RandomWallpaperExtension extends Extension {
    enable() {
        console.log('[random-wallpaper] extension enable()');
        this._indicator = new Indicator(this.path);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
