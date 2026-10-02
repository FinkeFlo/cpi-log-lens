import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../frontend/js/browse-url.js', import.meta.url));
const { NEWEST, browseHash, emptyFilters, entryCursor, parseBrowseHash, toUtcDateTime } = await import(
  `data:text/javascript;base64,${source.toString('base64')}`
);

const filters = {
  ...emptyFilters(),
  tenant: 'dev',
  level: 'DEBUG',
  iflow: 'Integration_Flow_With_A_Long_Exact_Name',
  grep: 'Connection REFUSED',
  date_from: '2026-09-30T12:34:56',
  date_to: '2026-10-01T14:00:00',
};
for (const position of [
  NEWEST,
  { kind: 'cursor', value: 'o20261001T120000_9876' },
  { kind: 'cursor', value: 'n20261001T120000.250000_1' },
  { kind: 'cursor', value: 'n20261001T120000_0' },
  { kind: 'at', value: '2026-10-01T08:00:00' },
  { kind: 'page', value: 4 },
]) {
  const hash = browseHash({ filters, position, entry: 9876 });
  assert.deepEqual(parseBrowseHash(hash), { filters, position, entry: 9876 });
}
assert.deepEqual(parseBrowseHash('#browse?page=1').position, NEWEST, 'page 1 is the newest page');
assert.deepEqual(parseBrowseHash('#browse?at=2026-10-01').position, { kind: 'at', value: '2026-10-01T23:59:59' });
assert.deepEqual(parseBrowseHash('#fetch'), null);
assert.equal(parseBrowseHash('#browse?date_from=2026-10-01')?.filters.date_from, '2026-10-01T00:00:00');
assert.equal(parseBrowseHash('#browse?date_to=2026-10-01')?.filters.date_to, '2026-10-01T23:59:59');
assert.equal(
  parseBrowseHash('#browse?date_from=2026-10-01T12%3A00%3A00%2B02%3A00')?.filters.date_from,
  '2026-10-01T10:00:00',
);
assert.equal(
  toUtcDateTime(parseBrowseHash('#browse?date_from=2026-10-01T12%3A00%3A00%2B02%3A00').filters.date_from, 'date_from'),
  '2026-10-01T10:00:00',
);
assert.equal(entryCursor('o', { id: 12, timestamp: '2026-10-01T12:00:00' }), 'o20261001T120000_12');
assert.equal(entryCursor('a', { id: 12, timestamp: '2026-10-01T12:00:00.5' }), 'a20261001T120000.5_12');
assert.equal(entryCursor('n', { id: 12 }), null, 'no cursor without a timestamp');

for (const invalid of [
  '#browse?level=TRACE',
  '#browse?page=0',
  '#browse?entry=-1',
  '#browse?unknown=x',
  '#browse?level=ERROR&level=DEBUG',
  '#browse?date_from=2026-02-30',
  '#browse?date_from=0000-01-01',
  '#browse?date_from=2026-10-02T00%3A00%3A00&date_to=2026-10-01T00%3A00%3A00',
  '#browse?cursor=x20261001T120000_1',
  '#browse?cursor=o2026-10-01_1',
  '#browse?cursor=o20261001T120000_0',
  '#browse?at=yesterday',
  '#browse?page=2&cursor=o20261001T120000_1',
  '#browse?at=2026-10-01&cursor=o20261001T120000_1',
]) assert.throws(() => parseBrowseHash(invalid), /Invalid Browse URL/, invalid);
assert.throws(
  () => browseHash({ filters: { ...emptyFilters(), iflow: 'x'.repeat(501) } }),
  /Invalid Browse URL/,
);

// A fake of GET /api/logs over `rows` (newest first), paging like the server: page numbers,
// cursors (o: older than, n: newer than, a: from an entry on), at=<time>, count=false.
const compact = timestamp => timestamp.replace(/[-:]/g, '');
const iso = stamp => `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 11)}:${stamp.slice(11, 13)}:${stamp.slice(13)}`;
const after = (row, ts, id) => row.timestamp > ts || (row.timestamp === ts && row.id > id);
function fakeSearch(rows, query) {
  const size = Number(query.page_size);
  let start = 0;
  let newest = false;
  let number = null;
  let boundary = null; // the newer entries of an empty page
  if (query.cursor) {
    const [, kind, stamp, idText] = /^([ona])(\d{8}T\d{6})_(\d+)$/.exec(query.cursor);
    const ts = iso(stamp);
    const id = Number(idText);
    if (kind === 'n') {
      const newer = rows.filter(row => after(row, ts, id)).length;
      if (newer <= size) newest = true;
      else start = newer - size;
    } else {
      start = rows.filter(row => after(row, ts, kind === 'o' ? id - 1 : id)).length;
      boundary = `n${stamp}_${kind === 'o' ? id - 1 : id}`;
    }
  } else if (query.at) {
    start = rows.filter(row => row.timestamp > query.at).length;
    boundary = `n${compact(query.at)}_9223372036854775807`;
  } else {
    number = query.page || 1;
    start = (number - 1) * size;
  }
  if (newest) {
    number = 1;
    start = 0;
  }
  const items = rows.slice(start, start + size);
  const counted = query.count !== false;
  return {
    total: counted ? rows.length : null,
    page: number,
    page_size: size,
    offset: items.length && (counted || number) ? start : null,
    newer_cursor: start > 0 ? (items.length ? `n${compact(items[0].timestamp)}_${items[0].id}` : boundary) : null,
    older_cursor: start + size < rows.length ? `o${compact(items.at(-1).timestamp)}_${items.at(-1).id}` : null,
    items,
  };
}
// ids 1000 … 1000 - n + 1, one minute apart, newest first
const makeRows = n => Array.from({ length: n }, (_, i) => ({
  id: 1000 - i,
  timestamp: `2026-10-01T${String(23 - Math.floor(i / 60)).padStart(2, '0')}:${String(59 - (i % 60)).padStart(2, '0')}:00`,
  message: `m${1000 - i}`,
}));

const requests = [];
const notifications = [];
const historyWrites = [];
const location = { origin: 'http://browse.test', pathname: '/', hash: '' };
const storage = new Map();
globalThis.window = { location };
globalThis.localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
};
globalThis.__browseTestRequests = requests;
globalThis.__rows = makeRows(1);
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const apiModule = moduleUrl(`
  export const api = {
    logs: {
      search: async query => {
        globalThis.__browseTestRequests.push({ path: 'logs', query });
        if (globalThis.__browseSearch) return globalThis.__browseSearch(query);
        return globalThis.__fakeSearch(globalThis.__rows, query);
      },
      iflows: async tenant => {
        globalThis.__browseTestRequests.push({ path: 'iflows', tenant });
        return { items: ['Integration_Flow_With_A_Long_Exact_Name'] };
      },
      get: async id => {
        globalThis.__browseTestRequests.push({ path: 'entry', id });
        if (globalThis.__browseGet) return globalThis.__browseGet(id);
        const row = globalThis.__rows.find(item => item.id === id);
        return { ...row, id, raw_line: 'raw log line' };
      },
    },
    db: { info: async () => ({ entries: globalThis.__browseDbEntries }) },
  };
`);
globalThis.__fakeSearch = fakeSearch;
const eventsModule = moduleUrl("export const emit = () => {}; export const OPEN_TENANT_MODAL = 'open-tenant-modal';");
const formatModule = moduleUrl('export const shortIflow = value => value;');
const urlModule = moduleUrl(source.toString());
const statesModule = moduleUrl(await readFile(new URL('../frontend/js/states.js', import.meta.url), 'utf8'));
const pageSource = (await readFile(new URL('../frontend/js/pages/browse.js', import.meta.url), 'utf8'))
  .replace("'../api.js'", `'${apiModule}'`)
  .replace("'../browse-url.js'", `'${urlModule}'`)
  .replace("'../events.js'", `'${eventsModule}'`)
  .replace("'../format.js'", `'${formatModule}'`)
  .replace("'../states.js'", `'${statesModule}'`)
  .replace("'../clipboard.js'", `'${moduleUrl('export const copyText = async () => true;')}'`);
const { default: browsePage } = await import(moduleUrl(pageSource));
function newPage(hash, { pageSize } = {}) {
  location.hash = hash;
  const stack = [hash];
  const page = browsePage();
  if (pageSize) page.pageSize = pageSize;
  page.$refs = {};
  page.$store = {
    route: {
      page: 'browse',
      writeHash(value, replace = false) {
        if (location.hash === value) return false; // like stores/route.js
        historyWrites.push({ value, replace });
        if (replace) stack[stack.length - 1] = value;
        else stack.push(value);
        location.hash = value;
      },
      // history.back() of this page's own history, then the page-shown event
      back() {
        historyWrites.push({ back: true });
        stack.pop();
        location.hash = stack.at(-1);
        page.onPageShown('browse');
      },
    },
    toast: { notify: message => notifications.push(message) },
    tenants: { list: [] },
  };
  return page;
}
const settle = () => new Promise(resolve => setTimeout(resolve, 10));
const lastLogsQuery = () => requests.filter(request => request.path === 'logs').at(-1).query;
const ids = page => page.logs.items.map(item => item.id);

// A link of an earlier version with a page number shows that page, then pins it to its first
// entry, so refreshing it doesn't shift it.
globalThis.__rows = makeRows(9);
const restored = newPage(
  '#browse?tenant=demo&level=ERROR&iflow=Integration_Flow_With_A_Long_Exact_Name&page=2&entry=996',
  { pageSize: 3 },
);
restored.init();
await settle();
assert.ok(requests.some(request => request.path === 'logs' && request.query.page === 2));
assert.deepEqual(ids(restored), [997, 996, 995]);
assert.deepEqual(restored.position, { kind: 'cursor', value: 'a20261001T235600_997' });
assert.ok(location.hash.includes('cursor=a20261001T235600_997') && !location.hash.includes('page='), location.hash);
assert.equal(restored.detailOpen, true, 'a deep link opens the entry drawer');
assert.equal(restored.detail?.id, 996);
assert.equal(restored.detail?.raw_line, 'raw log line');
assert.equal(restored.detailPosition, '5 of 9');
restored.onLogsChanged({ firstPage: false, visibleOnly: true });
await settle();
assert.equal(lastLogsQuery().cursor, 'a20261001T235600_997', 'an auto-refresh reloads the same page');
assert.equal(restored.detailOpen, true, 'an auto-refresh keeps the drawer open');
assert.ok(location.hash.includes('entry=996'), 'and keeps the entry in the URL');
restored.closeDetail();
assert.equal(restored.detailOpen, false);
assert.ok(location.hash.includes('cursor='));
assert.ok(!location.hash.includes('entry='));

const browseWritesBeforeOtherPage = historyWrites.length;
const otherPage = newPage('#fetch');
otherPage.$store.route.page = 'fetch';
otherPage.init();
await settle();
otherPage.onLogsChanged({ firstPage: true, visibleOnly: false });
await settle();
assert.equal(location.hash, '#fetch');
assert.equal(historyWrites.length, browseWritesBeforeOtherPage);

requests.length = 0;
const immediate = newPage('#browse?tenant=demo&cursor=o20261001T235700_997');
immediate.init();
await settle();
immediate.q.level = 'ERROR';
immediate.filtersChanged();
await settle();
assert.deepEqual(immediate.position, NEWEST, 'new filters start at the newest entries');
assert.ok(requests.some(request => request.path === 'logs' && request.query.level === 'ERROR' && !request.query.cursor));

immediate.q.iflow = 'Integration_Flow_With_A_Long_Exact_Name';
immediate.filtersChanged();
await settle();
immediate.q.grep = 'connection refused';
immediate.filtersChanged();
await settle();
assert.equal(immediate.iflowOptions[0], 'Integration_Flow_With_A_Long_Exact_Name');
assert.equal(Date.parse(immediate.q.date_to) - Date.parse(immediate.q.date_from), 24 * 60 * 60 * 1000);
assert.ok(location.hash.includes('iflow=Integration_Flow_With_A_Long_Exact_Name'));
assert.ok(location.hash.includes('date_from='));
assert.ok(historyWrites.some(write => !write.replace));
assert.deepEqual(notifications, []);

// Newer, older and newest: steps leave out the count; the range follows from the page before.
requests.length = 0;
globalThis.__rows = makeRows(8);
const paging = newPage('#browse', { pageSize: 3 });
paging.init();
await settle();
assert.equal(paging.rangeText, '1–3 of 8 entries');
await paging.goOlder();
assert.deepEqual(ids(paging), [997, 996, 995]);
assert.equal(lastLogsQuery().count, false, 'a step to the next page skips the count');
assert.equal(lastLogsQuery().cursor, 'o20261001T235700_998');
assert.equal(paging.rangeText, '4–6 of 8 entries');
assert.equal(paging.pageStatus, 'Showing 4–6 of 8 entries');
assert.ok(location.hash.includes('cursor=o20261001T235700_998'));
await paging.goOlder();
assert.deepEqual(ids(paging), [994, 993]);
assert.equal(paging.rangeText, '7–8 of 8 entries');
assert.equal(paging.logs.older_cursor, null);
await paging.goNewer();
assert.deepEqual(ids(paging), [997, 996, 995]);
assert.equal(paging.rangeText, '4–6 of 8 entries');
await paging.goNewest();
assert.deepEqual(ids(paging), [1000, 999, 998]);
assert.notEqual(lastLogsQuery().count, false, 'the newest page is counted again');
assert.equal(location.hash, '#browse');
// Back and forward: the URL of each step shows that page again.
location.hash = '#browse?cursor=o20261001T235700_998';
paging.onPageShown('browse');
await settle();
assert.deepEqual(ids(paging), [997, 996, 995]);

// Jump to time: the newest entry at or before it, counted; before all entries, nothing here.
paging.jumpTime = '2026-10-01T23:55:30';
await paging.jumpToTime();
assert.equal(lastLogsQuery().at, '2026-10-01T23:55:30');
assert.deepEqual(ids(paging), [996, 995, 994]);
assert.equal(paging.rangeText, '5–7 of 8 entries');
assert.ok(location.hash.includes('at=2026-10-01T23%3A55%3A30'));
paging.jumpTime = '2026-09-01T00:00:00';
await paging.jumpToTime();
assert.equal(paging.listState, 'nothing-here');
assert.equal(paging.pageStatus, 'No entries on this page');
await paging.goNewer();
assert.deepEqual(ids(paging), [995, 994, 993], 'newer than a time before all entries: the oldest entries');
assert.equal(paging.logs.older_cursor, null);

// Page size: stored for the next visit; the page keeps its first entry.
await paging.goNewest();
await paging.goOlder();
paging.pageSize = 5;
paging.pageSizeChanged();
await settle();
assert.equal(storage.get('cpi-log-lens-page-size'), '5');
assert.equal(lastLogsQuery().page_size, 5);
assert.deepEqual(ids(paging), [997, 996, 995, 994, 993]);
assert.equal(newPage('#browse').pageSize, 100, 'only the offered sizes are restored');
storage.set('cpi-log-lens-page-size', '250');
assert.equal(newPage('#browse').pageSize, 250);

// Result states: no data yet, no matches, and the three kinds of failed requests.
const noRows = query => ({ total: 0, page: 1, page_size: query.page_size, offset: null, newer_cursor: null, older_cursor: null, items: [] });
globalThis.__browseSearch = noRows;
globalThis.__browseDbEntries = 0;
const empty = newPage('#browse');
empty.init();
assert.equal(empty.listState, 'loading');
await settle();
assert.equal(empty.listState, 'empty', 'no filters and no rows: the database is empty');
const filtered = newPage('#browse?level=ERROR');
filtered.init();
await settle();
assert.equal(filtered.listState, 'empty', 'filters on an empty database still say "no data yet"');
globalThis.__browseDbEntries = 5;
filtered.q.level = 'WARN';
filtered.filtersChanged();
await settle();
assert.equal(filtered.listState, 'no-matches');

const failures = [
  [{ kind: 'unreachable', message: "Can't reach the server" }, 'unreachable', "Can't reach CPI Log Lens"],
  [{ kind: 'busy', retryAfter: 5, message: 'database busy: no free read connection' }, 'busy', 'The database is busy'],
  [{ kind: 'failed', message: 'Internal server error' }, 'failed', "Couldn't load log entries"],
];
for (const [error, kind, title] of failures) {
  globalThis.__browseSearch = () => { throw error; };
  const failing = newPage('#browse');
  failing.init();
  await settle();
  assert.equal(failing.listState, 'error');
  assert.equal(failing.listError.kind, kind);
  assert.equal(failing.listError.title, title);
}
assert.match(
  (await import(statesModule)).describeError(failures[1][0]).text,
  /Try again in 5 seconds/,
  'a busy answer names its Retry-After',
);

globalThis.__browseSearch = undefined;
const refreshed = newPage('#browse');
refreshed.init();
await settle();
assert.equal(refreshed.listState, 'results');
globalThis.__browseSearch = () => { throw failures[1][0]; };
refreshed.onLogsChanged({ firstPage: false, visibleOnly: true });
await settle();
assert.equal(refreshed.listState, 'results', 'a failed background refresh keeps the loaded list');
assert.equal(refreshed.refreshError.kind, 'busy');
globalThis.__browseSearch = undefined;
refreshed.retry();
await settle();
assert.equal(refreshed.refreshError, null);
assert.deepEqual(notifications, [], 'list states are shown in the page, not as toasts');

// Drawer: an entry that is not on the loaded page, previous/next across pages, a deleted entry.
globalThis.__rows = makeRows(9);
const drawer = newPage('#browse?entry=993', { pageSize: 3 });
drawer.init();
await settle();
assert.equal(drawer.detail.raw_line, 'raw log line', 'a deep-linked entry is loaded even when it is not on the page');
assert.equal(drawer.detailIndex, -1);
assert.equal(drawer.detailPosition, 'Not in the current list');
assert.equal(drawer.canStepNewer && drawer.canStepOlder, true, 'it can still step to its neighbours');
await drawer.stepEntry(1);
assert.equal(drawer.detail.id, 992, 'next of an entry that is not on the page');
assert.deepEqual(ids(drawer), [992]);
assert.equal(drawer.detailPosition, '9 of 9');
assert.equal(drawer.canStepOlder, false);
await drawer.stepEntry(-1);
assert.equal(drawer.detail.id, 993, 'previous at the start of a page loads the page before');
assert.deepEqual(ids(drawer), [995, 994, 993]);
assert.equal(drawer.detailPosition, '8 of 9');
await drawer.showEntry(drawer.logs.items[0]);
await drawer.stepEntry(-1);
assert.equal(drawer.detail.id, 996);
assert.deepEqual(ids(drawer), [998, 997, 996]);
assert.equal(drawer.detailPosition, '5 of 9', 'the position continues across pages without a count');
assert.ok(location.hash.includes('cursor=n20261001T235400_995') && location.hash.includes('entry=996'), location.hash);
await drawer.stepEntry(1);
assert.equal(drawer.detail.id, 995, 'next at the end of a page loads the next page');
assert.deepEqual(ids(drawer), [995, 994, 993]);
await drawer.stepEntry(-1);
await drawer.stepEntry(-1);
await drawer.stepEntry(-1);
assert.equal(drawer.detail.id, 998);
await drawer.stepEntry(-1);
assert.equal(drawer.detail.id, 999, 'previous near the top: the newest page holds the entry and its neighbour');
assert.deepEqual(ids(drawer), [1000, 999, 998]);
assert.equal(drawer.detailOpen, true);
// Next of an entry outside the list with nothing older: the list stays, the button turns off.
globalThis.__browseGet = id => ({ id, timestamp: '2026-09-01T00:00:00', raw_line: `raw ${id}` });
await drawer.openEntry(5);
await drawer.stepEntry(1);
assert.deepEqual(ids(drawer), [1000, 999, 998], 'an empty neighbour page does not replace the list');
assert.equal(drawer.detail.id, 5);
assert.equal(drawer.canStepOlder, false);
globalThis.__browseGet = undefined;

// A refresh while the drawer shows an entry of the newest page keeps that page where it is.
const live = newPage('#browse', { pageSize: 3 });
live.init();
await settle();
await live.showEntry(live.logs.items[2]);
globalThis.__rows = [
  ...[1003, 1002, 1001].map((id, i) => ({ id, timestamp: `2026-10-02T00:0${2 - i}:00`, message: `m${id}` })),
  ...makeRows(9),
];
live.onLogsChanged({ firstPage: true, visibleOnly: true });
await settle();
assert.deepEqual(ids(live), [1000, 999, 998], 'new entries do not push the open entry off the page');
assert.equal(live.detailIndex, 2);
assert.equal(live.rangeText, '4–6 of 12 entries');
assert.ok(live.logs.newer_cursor, 'the newer entries are one step away');
live.closeDetail();
live.onLogsChanged({ firstPage: true, visibleOnly: true });
await settle();
assert.equal(ids(live)[0], 1003, 'without the drawer, a finished fetch shows the newest entries');

// Next across a page boundary while a background refresh (a fetch imports entries) is in flight,
// whichever answer arrives first: the drawer shows the first entry of the next page.
globalThis.__rows = makeRows(9);
for (const refreshFirst of [false, true]) {
  const racing = newPage('#browse', { pageSize: 3 });
  racing.init();
  await settle();
  await racing.showEntry(racing.logs.items[2]);
  assert.equal(racing.detail.id, 998);
  const pending = [];
  globalThis.__browseSearch = query => new Promise(resolve => pending.push({ query, resolve }));
  const step = racing.stepEntry(1);
  await settle();
  racing.onLogsChanged({ firstPage: false, visibleOnly: true });
  await settle();
  assert.deepEqual(pending.map(request => request.query.cursor), ['o20261001T235700_998', 'o20261001T235700_998']);
  const answer = request => request.resolve(fakeSearch(globalThis.__rows, request.query));
  if (refreshFirst) answer(pending[1]);
  answer(pending[0]);
  await settle();
  if (!refreshFirst) answer(pending[1]);
  await step;
  await settle();
  assert.equal(racing.detail.id, 997, `Next shows the first entry of the next page (refresh answered ${refreshFirst ? 'first' : 'last'})`);
  assert.ok(location.hash.includes('cursor=o20261001T235700_998') && location.hash.includes('entry=997'), location.hash);
  assert.deepEqual(ids(racing), [997, 996, 995]);
  assert.equal(racing.detailPosition, '4 of 9');
  globalThis.__browseSearch = undefined;
}

// Closing the drawer undoes its own history entry, so Back afterwards does not reopen it.
const closing = newPage('#browse?cursor=o20261001T235700_998', { pageSize: 3 });
closing.init();
await settle();
const before = location.hash;
await closing.showEntry(closing.logs.items[0]);
assert.ok(location.hash.includes('entry='));
const writes = historyWrites.length;
closing.closeDetail();
await settle();
assert.deepEqual(historyWrites.slice(writes), [{ back: true }], 'closing goes back instead of adding an entry');
assert.equal(location.hash, before);
assert.equal(closing.detailOpen, false);
// After Next moved the list to another page, going back would change the list: replace instead.
await closing.showEntry(closing.logs.items[2]);
await closing.stepEntry(1);
assert.deepEqual(ids(closing), [994, 993, 992]);
const writesAfterStep = historyWrites.length;
closing.closeDetail();
assert.deepEqual(historyWrites.slice(writesAfterStep), [{ value: '#browse?cursor=o20261001T235400_995', replace: true }]);
assert.equal(closing.detailOpen, false);
// A copied link pins the newest page to its first entry.
const linking = newPage('#browse', { pageSize: 3 });
linking.init();
await settle();
await linking.showEntry(linking.logs.items[1]);
assert.equal(linking.entryLink(), 'http://browse.test/#browse?cursor=a20261001T235900_1000&entry=999');

globalThis.__browseGet = () => { throw { status: 404, kind: 'failed', message: 'Entry not found' }; };
await drawer.openEntry(42);
assert.equal(drawer.detailError.title, 'Log entry not found');
globalThis.__browseSearch = undefined;
globalThis.__browseGet = undefined;

console.log('Browse URL restoration (positions, page links of earlier versions), immediate filters, default text window and validation passed');
console.log('Paging (newer, older, newest without counting, back/forward, jump to time, page size) passed');
console.log('Log entry drawer (deep link, refresh keeps it and its page, previous/next across pages, missing entry) passed');
console.log('Browse result states (empty, no matches, nothing at a position, unreachable, busy, failed, background refresh) passed');
