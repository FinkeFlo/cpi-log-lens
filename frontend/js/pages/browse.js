// Browse page: URL-backed filters, page-based log results and the log entry drawer.
// The result list shows one state at a time: loading, results, no data yet, no matches,
// or an error (server unreachable, database busy, request failed). The drawer shows one
// entry (deep link: `entry` in the URL) independently of the list, so refreshing the list
// while a fetch imports new entries leaves it open.
import { api } from '../api.js';
import { browseHash, emptyFilters, parseBrowseHash, toUtcDateTime } from '../browse-url.js';
import { copyText } from '../clipboard.js';
import { emit, OPEN_TENANT_MODAL } from '../events.js';
import { shortIflow } from '../format.js';
import { describeError } from '../states.js';

const COPY_LABELS = { message: 'Message', raw: 'Raw line', link: 'Link' };

const emptyLogs = () => ({ items: [], total: 0, page: 1, pages: 1, page_size: 100 });

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

export default () => ({
  q: emptyFilters(),
  logs: emptyLogs(),
  currentPage: 1,
  loading: false,
  loadedOnce: false, // a search has answered since the page was opened
  listError: null, // describeError() of the last failed search; the list is empty then
  refreshError: null, // a failed background refresh; the last loaded list stays
  dbEmpty: false, // no entries at all (not just none for the filters)
  demoStarting: false,
  iflowOptions: [],
  urlError: '',
  // Log entry drawer: `detail` stays set after closing, so the closing dialog keeps its content.
  detail: null,
  detailOpen: false,
  detailLoading: false,
  detailError: null,
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

  // loading | results | empty (no entries at all) | no-matches | invalid (URL) | error
  get listState() {
    if (this.listError) return 'error';
    if (this.logs.items.length) return 'results';
    if (this.urlError && !this.loading) return 'invalid';
    if (this.loading || !this.loadedOnce) return 'loading';
    return this.dbEmpty ? 'empty' : 'no-matches';
  },

  init() {
    try {
      const state = parseBrowseHash(window.location.hash) || { filters: emptyFilters(), page: 1, entry: null };
      this.q = state.filters;
      this.currentPage = state.page;
      this.ensureSearchWindow();
      if (!validRange(this.q)) throw new TypeError('Invalid Browse URL: date_from must not be after date_to');
      if (this.$store.route.page === 'browse') this.writeUrl(state.page, state.entry, true);
      this.loadIflows();
      if (state.entry !== null && this.$store.route.page === 'browse') this.openEntry(state.entry);
      this.search(state.page, { writeUrl: false });
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
  writeUrl(page = this.currentPage, entry = this.detailOpen ? this.detail?.id ?? null : null, replace = false) {
    if (this.$store.route.page !== 'browse') return false;
    return this.$store.route.writeHash(browseHash({ filters: this.q, page, entry }), replace) !== false && !replace;
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
    this.currentPage = 1;
    this.detailOpen = false;
    this.ensureSearchWindow();
    if (!validRange(this.q)) {
      this.urlError = 'From must be earlier than or equal to To.';
      this.$store.toast.notify(this.urlError, 'error');
      return;
    }
    this.writeUrl(1, null);
    if (this._lastTenant !== this.q.tenant) {
      this._lastTenant = this.q.tenant;
      this.loadIflows();
    }
    this.search(1, { writeUrl: false });
  },

  setQuickRange(minutes) {
    const end = new Date();
    this.q.date_to = inputDate(end);
    this.q.date_from = inputDate(new Date(end.getTime() - minutes * 60 * 1000));
    this.filtersChanged();
  },

  // background: a refresh the user did not ask for (new entries were imported); if it
  // fails, the last loaded list stays and a notice says it may be outdated. A search never
  // closes the drawer; whoever changes filters or pages does that.
  // Returns the page it loaded, also when a newer search replaced it in the list
  // (null if it failed).
  async search(page = this.currentPage, { writeUrl = true, background = false } = {}) {
    this.urlError = '';
    if (!validRange(this.q)) {
      this.urlError = 'From must be earlier than or equal to To.';
      this.$store.toast.notify(this.urlError, 'error');
      return null;
    }
    this.currentPage = page;
    if (writeUrl) this.writeUrl(page);
    const requestId = ++this._requestId;
    this.loading = true;
    try {
      const logs = await api.logs.search({
        page,
        page_size: 100,
        ...this.q,
        date_from: toUtcDateTime(this.q.date_from, 'date_from'),
        date_to: toUtcDateTime(this.q.date_to, 'date_to'),
      });
      if (requestId !== this._requestId) return logs;
      await this.checkEmpty(logs, requestId);
      if (requestId !== this._requestId) return logs;
      this.logs = logs;
      this.listError = null;
      this.refreshError = null;
      this.loadedOnce = true;
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
    if (logs.total > 0) {
      this.dbEmpty = false;
    } else if (!this.hasFilters) {
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
    this.search(this.currentPage, { writeUrl: false });
  },

  resetSearch() {
    this.q = emptyFilters();
    this.currentPage = 1;
    this.detailOpen = false;
    this.urlError = '';
    this.writeUrl(1, null);
    this.search(1, { writeUrl: false });
  },

  goPage(page) {
    this.currentPage = page;
    this.detailOpen = false;
    this.writeUrl(page, null);
    this.search(page, { writeUrl: false });
  },

  pageRange() {
    const { page, pages } = this.logs;
    const delta = 2;
    const range = [];
    for (let i = Math.max(1, page - delta); i <= Math.min(pages, page + delta); i++) range.push(i);
    return range;
  },

  // Entries were imported or deleted: reload the list; an open entry stays in the drawer.
  onLogsChanged({ firstPage, visibleOnly }) {
    if (visibleOnly && this.$store.route.page !== 'browse') return;
    const page = firstPage ? 1 : this.currentPage;
    this.currentPage = page;
    this.writeUrl(page, undefined, true);
    this.search(page, { writeUrl: false, background: true });
  },

  showIflow({ iflow, level = '' }) {
    this.q.iflow = iflow;
    this.q.level = level;
    this.filtersChanged();
  },

  // The URL changed (navigation, back/forward): show its filters, page and entry. When
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
          browseHash({ filters: state.filters, page: state.page }) === browseHash({ filters: this.q, page: this.currentPage });
        this.q = state.filters;
        this.currentPage = state.page;
        this.ensureSearchWindow();
        if (!validRange(this.q)) throw new TypeError('Invalid Browse URL: date_from must not be after date_to');
        this.writeUrl(state.page, state.entry, true);
        if (state.entry === null) this.detailOpen = false;
        else if (!this.detailOpen || this.detail?.id !== state.entry) this.openEntry(state.entry);
        if (sameList) return;
        this.loadIflows();
        this.search(state.page, { writeUrl: false });
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
    if (this.writeUrl(this.currentPage, row.id)) this._hashBeforeEntry = before;
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
    if (before !== null && before === browseHash({ filters: this.q, page: this.currentPage })) {
      this.$store.route.back();
    } else {
      this.writeUrl(this.currentPage, null, true);
    }
  },

  // Position of the open entry in the loaded list (-1: not on this page).
  get detailIndex() {
    return this.detail ? this.logs.items.findIndex(item => item.id === this.detail.id) : -1;
  },

  get canStepNewer() {
    return this.detailIndex > 0 || (this.detailIndex === 0 && this.logs.page > 1);
  },

  get canStepOlder() {
    const index = this.detailIndex;
    return index >= 0 && (index < this.logs.items.length - 1 || this.logs.page < this.logs.pages);
  },

  get detailPosition() {
    const index = this.detailIndex;
    if (index < 0) return this.loadedOnce ? 'Not in the current list' : '';
    const position = (this.logs.page - 1) * this.logs.page_size + index + 1;
    return `${position.toLocaleString('en-US')} of ${this.logs.total.toLocaleString('en-US')}`;
  },

  // Previous (-1, newer) or next (+1, older) entry of the list, across page boundaries.
  async stepEntry(delta) {
    const index = this.detailIndex;
    if (index < 0) return;
    let row = this.logs.items[index + delta];
    if (!row) {
      const page = this.logs.page + delta;
      if (page < 1 || page > this.logs.pages) return;
      // Take the entry from the page this search loaded: a refresh started meanwhile
      // (entries being imported) may answer first or last.
      const logs = await this.search(page, { writeUrl: false });
      if (!logs?.items.length) return;
      row = delta > 0 ? logs.items[0] : logs.items[logs.items.length - 1];
    }
    this.writeUrl(this.currentPage, row.id, true);
    await this.openEntry(row);
  },

  entryLink() {
    const hash = browseHash({ filters: this.q, page: this.currentPage, entry: this.detail.id });
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
