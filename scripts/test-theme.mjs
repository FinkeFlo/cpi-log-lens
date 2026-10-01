import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../frontend/js/stores/theme.js', import.meta.url));
const { default: theme, THEME_CHANGED } = await import(`data:text/javascript;base64,${source.toString('base64')}`);
const saved = new Map();
const events = [];
const media = { matches: false, addEventListener(_name, handler) { this.change = handler; } };

globalThis.localStorage = {
  getItem: key => saved.get(key) ?? null,
  setItem: (key, value) => saved.set(key, value),
  removeItem: key => saved.delete(key),
};
globalThis.document = { documentElement: { dataset: { theme: 'lens-light' } } };
globalThis.CustomEvent = class {
  constructor(type, options) { this.type = type; this.detail = options.detail; }
};
globalThis.window = {
  matchMedia: () => media,
  dispatchEvent: event => events.push(event),
};

theme.init();
assert.equal(theme.preference, 'system');
media.matches = true;
media.change();
assert.equal(document.documentElement.dataset.theme, 'lens-dark');
assert.equal(events.at(-1).type, THEME_CHANGED);
assert.equal(events.at(-1).detail, 'lens-dark');

theme.setPreference('light');
assert.equal(saved.get('cpi-log-lens-theme'), 'light');
media.matches = false;
media.change();
assert.equal(document.documentElement.dataset.theme, 'lens-light');
media.matches = true;
media.change();
assert.equal(document.documentElement.dataset.theme, 'lens-light');

theme.setPreference('dark');
assert.equal(saved.get('cpi-log-lens-theme'), 'dark');
theme.setPreference('system');
assert.equal(saved.has('cpi-log-lens-theme'), false);
assert.equal(document.documentElement.dataset.theme, 'lens-dark');
assert.throws(() => theme.setPreference('invalid'), TypeError);

saved.set('cpi-log-lens-theme', 'unexpected');
theme.init();
assert.equal(theme.preference, 'system');
console.log('Theme preference, persistence, OS changes and events passed');
