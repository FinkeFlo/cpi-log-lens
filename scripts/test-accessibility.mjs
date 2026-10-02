import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../', import.meta.url);
const read = path => readFile(new URL(path, root), 'utf8');
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const html = await read('frontend/index.html');

assert.match(html, /<nav id="primary-navigation"[^>]*aria-label="Primary navigation"/);
for (const page of ['browse', 'fetch', 'stats', 'settings']) {
  assert.match(html, new RegExp(`<a href="#${page}"`), `${page} has a real navigation link`);
  assert.match(html, new RegExp(`<div[^>]*x-show="\\$store\\.route\\.page==='${page}'"[^>]*>[\\s\\S]*?<h1`));
}
assert.match(html, /<main id="main-content"/);
assert.equal((html.match(/<h1\b/g) || []).length, 4);
const mobileToggle = html.match(/<button type="button" class="mobile-nav-toggle[\s\S]*?<\/button>/)[0];
assert.doesNotMatch(mobileToggle, /aria-label=/, 'the mobile toggle name comes from its visible label');
assert.match(mobileToggle, /<span x-text="\$store\.route\.mobileNavOpen \? 'Close' : 'Menu'"><\/span>/);
assert.match(html, /<button type="button" class="link link-hover text-left font-mono-xs"[\s\S]*?@click\.stop="showEntry\(row\)"/);
assert.match(html, /<dialog x-dialog="detailOpen"[^>]*aria-labelledby="log-detail-title"[^>]*@cancel\.prevent="closeDetail\(\)"/);
assert.doesNotMatch(html, /<tr[^>]*(?:@click|tabindex=)/);
assert.match(html, /<dialog x-dialog="tenantModal\.open"[^>]*aria-labelledby="tenant-dialog-title"[^>]*@cancel\.prevent/);
assert.doesNotMatch(html, /:class="[^"]*modal-open|modal-backdrop/);

const eventStub = moduleUrl(`
  export const PAGE_SHOWN = 'page-shown';
  export function emit(name, detail) { globalThis.__events.push({ name, detail }); }
`);
const routeSource = (await read('frontend/js/stores/route.js')).replace("'../events.js'", `'${eventStub}'`);
const { default: route } = await import(moduleUrl(routeSource));
const listeners = new Map();
globalThis.__events = [];
globalThis.window = {
  location: { hash: '#browse?level=ERROR' },
  addEventListener(name, handler) { listeners.set(name, handler); },
};
globalThis.document = { title: '' };
globalThis.history = {
  pushState(_state, _title, hash) { window.location.hash = hash; },
  replaceState(_state, _title, hash) { window.location.hash = hash; },
};
route.init();
assert.equal(document.title, 'Browse logs | CPI Log Lens');
route.navigate('fetch');
assert.equal(window.location.hash, '#fetch');
assert.equal(document.title, 'Fetch logs | CPI Log Lens');
assert.equal(__events.length, 1);
window.location.hash = '#stats';
listeners.get('popstate')();
listeners.get('hashchange')();
assert.equal(document.title, 'Statistics | CPI Log Lens');
assert.equal(__events.length, 2, 'back/forward event pair updates the page once');
route.mobileNavOpen = true;
window.location.hash = '#settings';
listeners.get('hashchange')();
assert.equal(route.mobileNavOpen, false);
assert.equal(document.title, 'Settings | CPI Log Lens');

const { registerDialogDirective } = await import(moduleUrl(await read('frontend/js/dialog.js')));
let directive;
const state = { open: false };
registerDialogDirective({ directive(_name, callback) { directive = callback; } });
let runEffect;
const focusEvents = [];
const trigger = { isConnected: true, focus() { focusEvents.push('trigger'); } };
const heading = { focus() { focusEvents.push('dialog'); } };
const listenersByName = new Map();
const element = {
  open: false,
  isConnected: true,
  querySelector() { return heading; },
  showModal() { this.open = true; },
  close() {
    this.open = false;
    listenersByName.get('close')();
  },
  addEventListener(name, handler) { listenersByName.set(name, handler); },
  removeEventListener() {},
  dispatchEvent(event) {
    if (event.type === 'cancel') {
      state.open = false;
      return true;
    }
    return false;
  },
};
globalThis.document.activeElement = trigger;
directive(element, { expression: 'open' }, {
  evaluateLater() { return callback => callback(state.open); },
  effect(callback) { runEffect = callback; callback(); },
  cleanup() {},
});
state.open = true;
runEffect();
assert.equal(element.open, true);
assert.deepEqual(focusEvents, ['dialog'], 'opening focuses the dialog heading');
listenersByName.get('click')({ target: heading });
assert.equal(element.open, true, 'clicking dialog contents does not dismiss it');
listenersByName.get('click')({ target: element });
assert.equal(state.open, false, 'backdrop dismissal uses the cancel event to update state');
runEffect();
assert.equal(element.open, false, 'backdrop dismissal uses the native dialog close method');
assert.deepEqual(focusEvents, ['dialog', 'trigger'], 'backdrop dismissal restores focus to the opener');
state.open = true;
runEffect();
state.open = false;
runEffect();
assert.deepEqual(focusEvents, ['dialog', 'trigger', 'dialog', 'trigger'], 'programmatic close still restores focus');
assert.match(html, /@cancel\.prevent="tenantModal\.open=false"/, 'Escape cancellation closes the tenant dialog');

// data-return-focus: closing focuses the named element (the row of the entry shown last).
const rowButton = { isConnected: true, focus() { focusEvents.push('row'); } };
globalThis.document.querySelector = selector => (selector === "[data-entry-id='5']" ? rowButton : null);
element.dataset = { returnFocus: "[data-entry-id='5']" };
state.open = true;
runEffect();
state.open = false;
runEffect();
assert.deepEqual(focusEvents.slice(-2), ['dialog', 'row'], 'closing focuses the data-return-focus target');
element.dataset = { returnFocus: "[data-entry-id='6']" };
state.open = true;
runEffect();
state.open = false;
runEffect();
assert.deepEqual(focusEvents.slice(-2), ['dialog', 'trigger'], 'without that target, focus returns to the opener');

const apiStub = moduleUrl(`
  export const api = { logs: { get: async id => ({ raw_line: 'raw ' + id }) } };
`);
const browseUrlStub = moduleUrl(`
  export const NEWEST = { kind: 'newest' };
  export const browseHash = () => '#browse';
  export const entryCursor = () => null;
  export const emptyFilters = () => ({ tenant: '', level: '', iflow: '', grep: '', date_from: '', date_to: '' });
  export const parseBrowseHash = () => null;
  export const toUtcDateTime = value => value;
`);
const browseEvents = moduleUrl("export const emit = () => {}; export const OPEN_TENANT_MODAL = 'open-tenant-modal';");
const formatStub = moduleUrl('export const shortIflow = value => value;');
const browseSource = (await read('frontend/js/pages/browse.js'))
  .replace("'../api.js'", `'${apiStub}'`)
  .replace("'../browse-url.js'", `'${browseUrlStub}'`)
  .replace("'../events.js'", `'${browseEvents}'`)
  .replace("'../format.js'", `'${formatStub}'`)
  .replace("'../states.js'", `'${moduleUrl(await read('frontend/js/states.js'))}'`)
  .replace("'../clipboard.js'", `'${moduleUrl('export const copyText = async () => true;')}'`);
const { default: browsePage } = await import(moduleUrl(browseSource));
const page = browsePage();
page.$store = {
  route: { page: 'browse', writeHash() {} },
  toast: { notify() {} },
};
page.$refs = {};
const row = { id: 17, timestamp: '2026-09-30T12:00:00Z', iflow: 'Demo' };
await page.showEntry(row);
assert.equal(page.detailOpen, true, 'keyboard button activation opens the log entry drawer');
assert.equal(page.detail.id, 17);
assert.equal(page.detail.raw_line, 'raw 17', 'the drawer loads the raw line');
console.log('Navigation, page titles, dialog focus and return focus, Escape wiring, and keyboard log opening passed');
