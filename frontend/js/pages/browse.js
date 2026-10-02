// Browse page: URL-backed filters, the log list paged by position, and the log entry drawer.
// The list shows a page at a position: the newest entries, a cursor (Newer/Older), a time
// (Jump to time), or a page number from a link of an earlier version (turned into a cursor once
// loaded). Stepping to the next page leaves out the count of all matching entries, which costs
// a scan of them; the range shown follows from the page before.
// The result list shows one state at a time: loading, results, no data yet, no matches,
// nothing at this position, or an error (server unreachable, database busy, request failed).
// The drawer shows one entry (deep link: `entry` in the URL) independently of the list, so
// refreshing the list while a fetch imports new entries leaves it open.
import { api } from '../api.js';
import { NEWEST, browseHash, emptyFilters, entryCursor, parseBrowseHash, toUtcDateTime } from '../browse-url.js';
import { copyText } from '../clipboard.js';
import { emit, OPEN_TENANT_MODAL } from '../events.js';
import { shortIflow } from '../format.js';
import { describeError } from '../states.js';

const COPY_LABELS = { message: 'Message', raw: 'Raw line', link: 'Link' };
const PAGE_SIZES = [50, 100, 250, 500];
const PAGE_SIZE_KEY = 'cpi-log-lens-page-size';

const emptyLogs = () => ({ items: [], total: 0, offset: null, newer_cursor: null, older_cursor: null });

function storedPageSize() {
  try {
    const size = Number(localStorage.getItem(PAGE_SIZE_KEY));
    return PAGE_SIZES.includes(size) ? size : 100;
  } catch {
    return 100;
  }
}

const inputDate = value => {
  const date = new Date(value);
  const pad = number => String(number).padStart(2, '0');
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
};

const dateMillis = value => {
  const normalized = value.length === 10 ? `${value}T00:00:00` : value;
  return new Date(`${normalized}Z`).getTime();
};

const validRange = q => !q.date_from || !q.date_to || dateMillis(q.date_from) <= dateMillis(q.date_to);

const cursorAt = (kind, entry) => {
  const value = entryCursor(kind, entry);
  return value ? { kind: 'cursor', value } : null;
};

// Query parameters of a list position for GET /api/logs.
const positionQuery = position => ({
  cursor: position.kind === 'cursor' ? position.value : undefined,
  at: position.kind === 'at' ? toUtcDateTime(position.value, 'at') : undefined,
  page: position.kind === 'page' ? position.value : undefined,
});

// An answer without counts (count=false), one page older or newer than `previous`: the older
// page starts right after it, the newer one ends right before it (the newest page starts at 0).
function continueCounts(logs, previous, step) {
  logs.total = previous.total;
  if (logs.offset !== null || !logs.items.length) return;
  if (!logs.newer_cursor) logs.offset = 0;
  else if (previous.offset === null) logs.offset = null;
  else if (step === 'older') logs.offset = previous.offset + previous.items.length;
  else logs.offset = Math.max(0, previous.offset - logs.items.length);
}

export default () => ({
  q: emptyFilters(),
  position: NEWEST,
  pageSize: storedPageSize(),
  jumpTime: '', // the Jump to time field (UTC)
  logs: emptyLogs(),
  loading: false,
  loadedOnce: false, // a search has answered since the page was opened
  listError: null, // describeError() of the last failed search; the list is empty then
  refreshError: null, // a failed background refresh; the last loaded list stays
  dbEmpty: false, // no entries at all (not just none for the filters)
  demoStarting: false,
  iflowOptions: [],
  urlError: '',
  pageStatus: '', // the range after the user changed the page, announced to screen readers
  // Log entry drawer: `detail` stays set after closing, so the closing dialog keeps its content.
  detail: null,
  detailOpen: false,
  detailLoading: false,
  detailError: null,
  detailEnd: { newer: false, older: false }, // stepping found no entry in that direction
  copied: '', // message | raw | link: the copy button that just succeeded
  copyStatus: '', // announced to screen readers
  _detailRequest: 0,
  _copyTimer: null,
  _hashBeforeEntry: null, // the URL before showEntry() added the entry to the history

  _requestId: 0,
  _filterRevision: 0,
  _iflowCache: new Map(),

  get tenants() {
    return this.$store.tenants.list;
  },

  get hasFilters() {
    return Object.entries(this.q).some(([key, value]) =>
      value && !(key === 'tenant' && value === 'all') && !(key === 'level' && value === 'ALL'));
  },

  // loading | results | empty (no entries at all) | no-matches | nothing-here (none at this
  // position, but elsewhere in the list) | invalid (URL) | error
  get listState() {
    if (this.listError) return 'error';
    if (this.logs.items.length) return 'results';
    if (this.urlError && !this.loading) return 'invalid';
    if (this.loading || !this.loadedOnce) return 'loading';
    if (this.position.kind !== 'newest' && this.logs.total !== 0) return 'nothing-here';
    return this.dbEmpty ? 'empty' : 'no-matches';
  },

  // "101–200 of 2,345 entries", or only the total when the position is not known.
  get rangeText() {
    const { items, total, offset } = this.logs;
    const number = value => value.toLocaleString('en-US');
    if (total === null) return '';
    const entries = `${number(total)} ${total === 1 ? 'entry' : 'entries'}`;
    if (offset === null || !items.length || (offset === 0 && items.length >= total)) return entries;
    return `${number(offset + 1)}–${number(offset + items.length)} of ${entries}`;
  },

  init() {
    try {
      const state = parseBrowseHash(window.location.hash) || { filters: emptyFilters(), position: NEWEST, entry: null };
      this.q = state.filters;
      this.position = state.position;
      if (state.position.kind === 'at') this.jumpTime = state.position.value;
      this.ensureSearchWindow();
      if (!validRange(this.q)) throw new TypeError('Invalid Browse URL: date_from must not be after date_to');
      if (this.$store.route.page === 'browse') this.writeUrl(state.position, state.entry, true);
      this.loadIflows();
      if (state.entry !== null && this.$store.route.page === 'browse') this.openEntry(state.entry);
      this.load(state.position, { writeUrl: false });
    } catch (error) {
      this.setUrlError(error);
    }
  },

  setUrlError(error) {
    this.urlError = error.message;
    this._requestId++;
    this.detailOpen = false;
    this.logs = emptyLogs();
    this.listError = null;
    this.loading = false;
    this.$store.toast.notify(this.urlError, 'error');
  },

  // Returns whether a new history entry was added.
  writeUrl(position = this.position, entry = this.detailOpen ? this.detail?.id ?? null : null, replace = false) {
    if (this.$store.route.page !== 'browse') return false;
    return this.$store.route.writeHash(browseHash({ filters: this.q, position, entry }), replace) !== false && !replace;
  },

  ensureSearchWindow() {
    if (!this.q.grep) return;
    const now = new Date();
    if (!this.q.date_from && !this.q.date_to) {
      this.q.date_to = inputDate(now);
      this.q.date_from = inputDate(new Date(now.getTime() - 24 * 60 * 60 * 1000));
    } else if (!this.q.date_from) {
      this.q.date_from = inputDate(new Date(dateMillis(this.q.date_to) - 24 * 60 * 60 * 1000));
    } else if (!this.q.date_to) {
      this.q.date_to = inputDate(now);
    }
  },

  async loadIflows() {
    const tenant = this.q.tenant || 'all';
    if (this._iflowCache.has(tenant)) {
      this.iflowOptions = this._iflowCache.get(tenant);
      return;
    }
    try {
      const { items } = await api.logs.iflows(tenant);
      this._iflowCache.set(tenant, items);
      if ((this.q.tenant || 'all') === tenant) this.iflowOptions = items;
    } catch {
      // Only the suggestions are missing; the list shows its own error state.
    }
  },

  async filtersChanged() {
    this.urlError = '';
    this._filterRevision++;
    this.position = NEWEST;
    this.detailOpen = false;
    this.ensureSearchWindow();
    if (!validRange(this.q)) {
      this.urlError = 'From must be earlier than or equal to To.';
      this.$store.toast.notify(this.urlError, 'error');
      return;
    }
    this.writeUrl(NEWEST, null);
    if (this._lastTenant !== this.q.tenant) {
      this._lastTenant = this.q.tenant;
      this.loadIflows();
    }
    this.load(NEWEST, { writeUrl: false });
  },

  setQuickRange(minutes) {
    const end = new Date();
    this.q.date_to = inputDate(end);
    this.q.date_from = inputDate(new Date(end.getTime() - minutes * 60 * 1000));
    this.filtersChanged();
  },

  // Load the page at `position`. background: a refresh the user did not ask for (new entries
  // were imported); if it fails, the last loaded list stays and a notice says it may be
  // outdated. count: false for a step to the next page (`step`: 'older' or 'newer'), whose
  // range follows from the current page. requireItems: keep the current list if the page is
  // empty. A load never closes the drawer; whoever changes filters or pages does that.
  // Returns the page it loaded, also when a newer load replaced it in the list (null if it
  // failed).
  async load(position = this.position, { writeUrl = true, background = false, count = true, step = null, requireItems = false } = {}) {
    this.urlError = '';
    if (!validRange(this.q)) {
      this.urlError = 'From must be earlier than or equal to To.';
      this.$store.toast.notify(this.urlError, 'error');
      return null;
    }
    const previous = this.logs;
    const previousPosition = this.position;
    this.position = position;
    if (writeUrl) this.writeUrl(position);
    const requestId = ++this._requestId;
    this.loading = true;
    try {
      const logs = await api.logs.search({
        ...this.q,
        date_from: toUtcDateTime(this.q.date_from, 'date_from'),
        date_to: toUtcDateTime(this.q.date_to, 'date_to'),
        page_size: this.pageSize,
        ...positionQuery(position),
        count: count ? undefined : false,
      });
      if (!count) continueCounts(logs, previous, step);
      if (requestId !== this._requestId) return logs;
      if (requireItems && !logs.items.length) {
        this.position = previousPosition;
        return logs;
      }
      await this.checkEmpty(logs, requestId);
      if (requestId !== this._requestId) return logs;
      this.logs = logs;
      this.listError = null;
      this.refreshError = null;
      this.loadedOnce = true;
      // A page number (links from earlier versions) becomes the cursor of its first entry, so
      // refreshing the page doesn't shift it while new entries arrive.
      if (position.kind === 'page' && logs.items.length) {
        this.position = cursorAt('a', logs.items[0]) || position;
        this.writeUrl(this.position, undefined, true);
      }
      return logs;
    } catch (error) {
      if (requestId !== this._requestId) return null;
      const state = describeError(error, 'log entries');
      if (background && this.logs.items.length) {
        this.refreshError = state;
      } else {
        this.listError = state;
        this.logs = emptyLogs();
      }
      return null;
    } finally {
      if (requestId === this._requestId) this.loading = false;
    }
  },

  // No results: tell an empty database ("no data yet") from filters that match nothing.
  async checkEmpty(logs, requestId) {
    if (logs.items.length || logs.total > 0) {
      this.dbEmpty = false;
    } else if (!this.hasFilters && logs.total === 0) {
      this.dbEmpty = true;
    } else {
      try {
        const info = await api.db.info();
        if (requestId === this._requestId) this.dbEmpty = info.entries === 0;
      } catch {
        if (requestId === this._requestId) this.dbEmpty = false; // unknown: say "no matches"
      }
    }
  },

  // "Try again" of the error states: reload what failed.
  retry() {
    if (this.$store.tenants.error) this.$store.tenants.load();
    if (!this.iflowOptions.length) this.loadIflows();
    this.load(this.position, { writeUrl: false });
  },

  resetSearch() {
    this.q = emptyFilters();
    this.position = NEWEST;
    this.detailOpen = false;
    this.urlError = '';
    this.writeUrl(NEWEST, null);
    this.load(NEWEST, { writeUrl: false });
  },

  // ── Paging ──

  goNewest() {
    return this.goTo(NEWEST);
  },

  goNewer() {
    return this.logs.newer_cursor ? this.goTo({ kind: 'cursor', value: this.logs.newer_cursor }, 'newer') : null;
  },

  goOlder() {
    return this.logs.older_cursor ? this.goTo({ kind: 'cursor', value: this.logs.older_cursor }, 'older') : null;
  },

  jumpToTime() {
    if (!this.jumpTime) return this.goTo(NEWEST);
    let value;
    try {
      value = toUtcDateTime(this.jumpTime, 'at');
    } catch {
      this.$store.toast.notify('Enter a valid date and time to jump to.', 'error');
      return null;
    }
    return this.goTo({ kind: 'at', value });
  },

  // The user moved to another part of the list: a new history entry; the drawer closes.
  async goTo(position, step = null) {
    this.detailOpen = false;
    this.writeUrl(position, null);
    const counted = this.logs.offset !== null && this.logs.total !== null;
    const request = this._requestId + 1; // the id load() gives its request
    const logs = await this.load(position, { writeUrl: false, step, count: !(step && counted) });
    if (!logs || this._requestId !== request) return; // failed, or replaced by a newer load
    this.pageStatus = logs.items.length ? `Showing ${this.rangeText}` : 'No entries on this page';
    if (this.$refs.logScroll) this.$refs.logScroll.scrollTop = 0;
    this.$refs.listTop?.scrollIntoView?.({ block: 'nearest' });
  },

  pageSizeChanged() {
    try {
      localStorage.setItem(PAGE_SIZE_KEY, String(this.pageSize));
    } catch {
      // Not stored: the size applies until the page is reloaded.
    }
    // Keep the first entry at the top: the page grows or shrinks at its end.
    const first = this.logs.items[0];
    const position = this.position.kind === 'newest' || !first ? this.position : cursorAt('a', first) || this.position;
    this.writeUrl(position, undefined, true);
    this.load(position, { writeUrl: false });
  },

  // Entries were imported or deleted: reload the list; an open entry stays in the drawer, and
  // the list stays where it is while the drawer shows one of its entries.
  onLogsChanged({ firstPage, visibleOnly }) {
    if (visibleOnly && this.$store.route.page !== 'browse') return;
    let position = this.position;
    if (this.detailOpen && this.detailIndex >= 0) {
      if (position.kind === 'newest') position = cursorAt('a', this.logs.items[0]) || position;
    } else if (firstPage && !this.detailOpen) {
      position = NEWEST;
    }
    this.writeUrl(position, undefined, true);
    this.load(position, { writeUrl: false, background: true });
  },

  showIflow({ iflow, level = '' }) {
    this.q.iflow = iflow;
    this.q.level = level;
    this.filtersChanged();
  },

  // The URL changed (navigation, back/forward): show its filters, position and entry. When
  // only the entry differs, the list is not loaded again.
  onPageShown(page) {
    this._hashBeforeEntry = null;
    if (page !== 'browse') {
      this.detailOpen = false;
      return;
    }
    const revision = this._filterRevision;
    queueMicrotask(() => {
      if (revision !== this._filterRevision) return;
      try {
        const state = parseBrowseHash(window.location.hash);
        if (!state) return;
        const sameList = this.loadedOnce && !this.listError &&
          browseHash({ filters: state.filters, position: state.position }) === browseHash({ filters: this.q, position: this.position });
        this.q = state.filters;
        this.position = state.position;
        if (state.position.kind === 'at') this.jumpTime = state.position.value;
        this.ensureSearchWindow();
        if (!validRange(this.q)) throw new TypeError('Invalid Browse URL: date_from must not be after date_to');
        this.writeUrl(state.position, state.entry, true);
        if (state.entry === null) this.detailOpen = false;
        else if (!this.detailOpen || this.detail?.id !== state.entry) this.openEntry(state.entry);
        if (sameList) return;
        this.loadIflows();
        this.load(state.position, { writeUrl: false });
      } catch (error) {
        this.setUrlError(error);
      }
    });
  },

  // ── Log entry drawer ──

  // A row was activated: open the drawer (a new history entry, so Back closes it).
  async showEntry(row) {
    const before = window.location.hash;
    this.detailOpen = true;
    if (this.writeUrl(this.position, row.id)) this._hashBeforeEntry = before;
    await this.openEntry(row);
  },

  // Show an entry: a row of the list at once, then the stored entry with its raw line.
  async openEntry(rowOrId) {
    const id = typeof rowOrId === 'object' ? rowOrId.id : rowOrId;
    const row = typeof rowOrId === 'object' ? rowOrId : this.logs.items.find(item => item.id === id);
    const request = ++this._detailRequest;
    this.detail = row ? { ...row } : { id };
    this.detailOpen = true;
    this.detailError = null;
    this.detailEnd = { newer: false, older: false };
    this.copied = '';
    this.copyStatus = '';
    this.detailLoading = true;
    try {
      const entry = await api.logs.get(id);
      if (request === this._detailRequest) this.detail = { ...this.detail, ...entry };
    } catch (error) {
      if (request !== this._detailRequest) return;
      this.detailError = error.status === 404
        ? { kind: 'missing', icon: 'search-x', title: 'Log entry not found',
            text: `Entry ${id} no longer exists. It may have been deleted by a cleanup or retention.`, detail: '' }
        : describeError(error, 'the log entry');
    } finally {
      if (request === this._detailRequest) this.detailLoading = false;
    }
  },

  // Close the drawer. If it added the history entry and the list is still the one it was
  // opened from, go back, so Back afterwards doesn't reopen it; otherwise drop the entry
  // from the URL in place.
  closeDetail() {
    if (!this.detailOpen) return;
    this.detailOpen = false;
    this._detailRequest++;
    const before = this._hashBeforeEntry;
    this._hashBeforeEntry = null;
    if (before !== null && before === browseHash({ filters: this.q, position: this.position })) {
      this.$store.route.back();
    } else {
      this.writeUrl(this.position, null, true);
    }
  },

  // Position of the open entry in the loaded list (-1: not on this page).
  get detailIndex() {
    return this.detail ? this.logs.items.findIndex(item => item.id === this.detail.id) : -1;
  },

  // An entry that is not in the list can still step: to the entries next to it in the list.
  get canStepNewer() {
    const index = this.detailIndex;
    if (index > 0) return true;
    if (index === 0) return Boolean(this.logs.newer_cursor);
    return Boolean(entryCursor('n', this.detail)) && !this.detailEnd.newer;
  },

  get canStepOlder() {
    const index = this.detailIndex;
    if (index >= 0 && index < this.logs.items.length - 1) return true;
    if (index >= 0) return Boolean(this.logs.older_cursor);
    return Boolean(entryCursor('o', this.detail)) && !this.detailEnd.older;
  },

  get detailPosition() {
    const index = this.detailIndex;
    if (index < 0) return this.loadedOnce ? 'Not in the current list' : '';
    const { offset, total } = this.logs;
    if (offset === null || total === null) return '';
    return `${(offset + index + 1).toLocaleString('en-US')} of ${total.toLocaleString('en-US')}`;
  },

  // Previous (-1, newer) or next (+1, older) entry of the list. At the end of the page, or for
  // an entry that is not in the list, the list moves to the page next to the entry.
  async stepEntry(delta) {
    const entry = this.detail;
    const index = this.detailIndex;
    let row = index >= 0 ? this.logs.items[index + delta] : undefined;
    if (!row) {
      const position = cursorAt(delta > 0 ? 'o' : 'n', entry);
      if (!position) return;
      // An entry at the edge of the page: the next page follows the current one directly.
      const step = index >= 0 ? (delta > 0 ? 'older' : 'newer') : null;
      const counted = this.logs.offset !== null && this.logs.total !== null;
      // Take the entry from the page this load returned: a refresh started meanwhile
      // (entries being imported) may answer first or last.
      const logs = await this.load(position, { writeUrl: false, step, count: !(step && counted), requireItems: true });
      if (!logs) return;
      // With fewer newer entries than a page, the newest page comes back, with the entry on it.
      const at = logs.items.findIndex(item => item.id === entry.id);
      row = at >= 0 ? logs.items[at + delta] : logs.items[delta > 0 ? 0 : logs.items.length - 1];
      if (!row) {
        this.detailEnd = { ...this.detailEnd, [delta > 0 ? 'older' : 'newer']: true };
        return;
      }
    }
    this.writeUrl(this.position, row.id, true);
    await this.openEntry(row);
  },

  // A link to the entry and the page it is shown with; the newest page is pinned to its
  // current first entry, so the link still shows the same page after new entries arrived.
  entryLink() {
    const first = this.logs.items[0];
    const position = this.position.kind === 'newest' && first ? cursorAt('a', first) || NEWEST : this.position;
    const hash = browseHash({ filters: this.q, position, entry: this.detail.id });
    return `${window.location.origin}${window.location.pathname}${hash}`;
  },

  async copy(kind) {
    const text = { message: this.detail?.message, raw: this.detail?.raw_line, link: this.entryLink() }[kind];
    if (!text) return;
    const ok = await copyText(text, this.$refs.detailDialog);
    this.copied = ok ? kind : '';
    this.copyStatus = ok ? `${COPY_LABELS[kind]} copied` : "Couldn't copy. Select the text and copy it manually.";
    clearTimeout(this._copyTimer);
    this._copyTimer = setTimeout(() => { this.copied = ''; }, 2000);
  },

  levelClass(level) {
    return ['ERROR', 'WARN', 'INFO', 'DEBUG'].includes(level) ? `level-${level.toLowerCase()}` : '';
  },

  connectTenant() {
    this.$store.route.navigate('settings');
    emit(OPEN_TENANT_MODAL);
  },

  async startDemo() {
    this.demoStarting = true;
    try {
      const data = await api.fetch.demo();
      await this.$store.tenants.load();
      this.$store.fetchJob.follow(data.job_id);
      this.$store.toast.notify('Importing demo data…', 'success');
    } catch (e) {
      this.$store.toast.notify(`Couldn't start the demo: ${e.message}`, 'error');
    } finally {
      this.demoStarting = false;
    }
  },

  shortIflow,
});
