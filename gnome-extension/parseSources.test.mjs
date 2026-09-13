// Run with: node --test gnome-extension/parseSources.test.mjs
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {parseSourceFile} from './parseSources.js';

test('parses tab-separated path and date per line', () => {
    const text = 'Camera1/a.JPG\t18 Mar 2026\nCamera2/trip/b.JPG\t15 Mar 2026\n';
    assert.deepEqual(parseSourceFile(text), [
        {path: 'Camera1/a.JPG', date: '18 Mar 2026'},
        {path: 'Camera2/trip/b.JPG', date: '15 Mar 2026'},
    ]);
});

test('ignores blank lines', () => {
    const text = '\na.JPG\t18 Mar 2026\n\n\nb.JPG\t15 Mar 2026\n\n';
    assert.equal(parseSourceFile(text).length, 2);
});

test('returns empty array for empty content', () => {
    assert.deepEqual(parseSourceFile(''), []);
    assert.deepEqual(parseSourceFile('\n\n'), []);
});

test('falls back to the whole line as the path when there is no tab', () => {
    assert.deepEqual(parseSourceFile('a.JPG\n'), [{path: 'a.JPG', date: ''}]);
});
