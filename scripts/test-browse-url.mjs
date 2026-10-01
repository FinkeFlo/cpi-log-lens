import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../frontend/js/browse-url.js', import.meta.url));
const { browseHash, emptyFilters, parseBrowseHash, toUtcDateTime } = await import(
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
const hash = browseHash({ filters, page: 4, entry: 9876 });
assert.deepEqual(parseBrowseHash(hash), { filters, page: 4, entry: 9876 });
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

for (const invalid of [
  '#browse?level=TRACE',
  '#browse?page=0',
  '#browse?entry=-1',
  '#browse?unknown=x',
  '#browse?level=ERROR&level=DEBUG',
  '#browse?date_from=2026-02-30',
  '#browse?date_from=0000-01-01',
  '#browse?date_from=2026-10-02T00%3A00%3A00&date_to=2026-10-01T00%3A00%3A00',
]) assert.throws(() => parseBrowseHash(invalid), /Invalid Browse URL/);
assert.throws(
  () => browseHash({ filters: { ...emptyFilters(), iflow: 'x'.repeat(501) } }),
  /Invalid Browse URL/,
);

const requests = [];
const notifications = [];
const historyWrites = [];
const location = { origin: 'http://browse.test', hash: '' };
globalThis.window = { location };
globalThis.__browseTestRequests = requests;
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const apiModule = moduleUrl(`
  export const api = {
    logs: {
      search: async query => {
        globalThis.__browseTestRequests.push({ path: 'logs', query });
        if (globalThis.__browseSearch) return globalThis.__browseSearch(query);
        return { total: 1, page: query.page, page_size: 100, pages: 1, items: [{ id: 123, iflow: 'Integration_Flow_With_A_Long_Exact_Name' }] };
      },
      iflows: async tenant => {
        globalThis.__browseTestRequests.push({ path: 'iflows', tenant });
        return { items: ['Integration_Flow_With_A_Long_Exact_Name'] };
      },
      get: async id => {
        globalThis.__browseTestRequests.push({ path: 'entry', id });
        return { raw_line: 'raw log line' };
      },
    },
    db: { info: async () => ({ entries: globalThis.__browseDbEntries }) },
  };
`);
const eventsModule = moduleUrl("export const emit = () => {}; export const OPEN_TENANT_MODAL = 'open-tenant-modal';");
const formatModule = moduleUrl('export const shortIflow = value => value;');
const urlModule = moduleUrl(source.toString());
const statesModule = moduleUrl(await readFile(new URL('../frontend/js/states.js', import.meta.url), 'utf8'));
const pageSource = (await readFile(new URL('../frontend/js/pages/browse.js', import.meta.url), 'utf8'))
  .replace("'../api.js'", `'${apiModule}'`)
  .replace("'../browse-url.js'", `'${urlModule}'`)
  .replace("'../events.js'", `'${eventsModule}'`)
  .replace("'../format.js'", `'${formatModule}'`)
  .replace("'../states.js'", `'${statesModule}'`);
const { default: browsePage } = await import(moduleUrl(pageSource));
function newPage(hash) {
  location.hash = hash;
  const page = browsePage();
  page.$store = {
    route: {
      page: 'browse',
      writeHash(value, replace = false) {
        historyWrites.push({ value, replace });
        location.hash = value;
      },
    },
    toast: { notify: message => notifications.push(message) },
    tenants: { list: [] },
  };
  return page;
}
const settle = () => new Promise(resolve => setTimeout(resolve, 10));

const restored = newPage(
  '#browse?tenant=demo&level=ERROR&iflow=Integration_Flow_With_A_Long_Exact_Name&page=2&entry=123',
);
restored.init();
await settle();
assert.equal(restored.currentPage, 2);
assert.equal(restored.selected?.id, 123);
assert.equal(restored.selected?.raw_line, 'raw log line');
assert.ok(requests.some(request => request.path === 'logs' && request.query.page === 2));
restored.closeRow();
assert.equal(restored.selected, null);
assert.ok(location.hash.includes('page=2'));
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
const immediate = newPage('#browse?tenant=demo');
immediate.init();
await settle();
immediate.q.level = 'ERROR';
immediate.filtersChanged();
await settle();
assert.equal(immediate.logs.page, 1);
assert.ok(requests.some(request => request.path === 'logs' && request.query.level === 'ERROR'));

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

// Result states: no data yet, no matches, and the three kinds of failed requests.
const noRows = query => ({ total: 0, page: query.page, page_size: 100, pages: 1, items: [] });
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

console.log('Browse URL restoration, immediate filters, default text window and validation passed');
console.log('Browse result states (empty, no matches, unreachable, busy, failed, background refresh) passed');
