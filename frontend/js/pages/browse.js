// Browse page: URL-backed filters, page-based log results and the existing inline detail.
import { api } from '../api.js';
import { browseHash, emptyFilters, parseBrowseHash, toUtcDateTime } from '../browse-url.js';
import { emit, OPEN_TENANT_MODAL } from '../events.js';
import { shortIflow } from '../format.js';

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
  logs: { items: [], total: 0, page: 1, pages: 1, page_size: 100 },
  currentPage: 1,
  selected: null,
  loading: false,
  demoStarting: false,
  iflowOptions: [],
  urlError: '',
  _requestId: 0,
  _filterRevision: 0,
  _iflowCache: new Map(),

  get tenants() {
    return this.$store.tenants.list;
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
      this.search(state.page, { restoreEntry: state.entry, writeUrl: false });
    } catch (error) {
      this.setUrlError(error);
    }
  },

  setUrlError(error) {
    this.urlError = error.message;
    this._requestId++;
    this.selected = null;
    this.logs = { items: [], total: 0, page: 1, pages: 1, page_size: 100 };
    this.loading = false;
    this.$store.toast.notify(this.urlError, 'error');
  },

  writeUrl(page = this.currentPage, entry = this.selected?.id ?? null, replace = false) {
    if (this.$store.route.page !== 'browse') return;
    this.$store.route.writeHash(browseHash({ filters: this.q, page, entry }), replace);
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
    } catch (error) {
      this.$store.toast.notify(`Couldn't load IFlow suggestions: ${error.message}`, 'error');
    }
  },

  async filtersChanged() {
    this.urlError = '';
    this._filterRevision++;
    this.currentPage = 1;
    this.selected = null;
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

  async search(page = this.currentPage, { restoreEntry = null, writeUrl = true } = {}) {
    this.urlError = '';
    if (!validRange(this.q)) {
      this.urlError = 'From must be earlier than or equal to To.';
      this.$store.toast.notify(this.urlError, 'error');
      return;
    }
    this.currentPage = page;
    if (writeUrl) this.writeUrl(page);
    const requestId = ++this._requestId;
    this.loading = true;
    this.selected = null;
    try {
      const logs = await api.logs.search({
        page,
        page_size: 100,
        ...this.q,
        date_from: toUtcDateTime(this.q.date_from, 'date_from'),
        date_to: toUtcDateTime(this.q.date_to, 'date_to'),
      });
      if (requestId !== this._requestId) return;
      this.logs = logs;
      if (restoreEntry !== null) {
        const row = logs.items.find(item => item.id === restoreEntry);
        if (!row) {
          this.urlError = `Entry ${restoreEntry} is not on page ${page} for these filters.`;
          this.$store.toast.notify(this.urlError, 'error');
          return;
        }
        await this.openRow(row);
      }
    } catch (error) {
      if (requestId === this._requestId) this.$store.toast.notify(`Couldn't load logs: ${error.message}`, 'error');
    } finally {
      if (requestId === this._requestId) this.loading = false;
    }
  },

  resetSearch() {
    this.q = emptyFilters();
    this.currentPage = 1;
    this.selected = null;
    this.urlError = '';
    this.writeUrl(1, null);
    this.search(1, { writeUrl: false });
  },

  goPage(page) {
    this.currentPage = page;
    this.selected = null;
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

  onLogsChanged({ firstPage, visibleOnly }) {
    if (visibleOnly && this.$store.route.page !== 'browse') return;
    const page = firstPage ? 1 : this.currentPage;
    this.currentPage = page;
    this.selected = null;
    this.writeUrl(page, null);
    this.search(page, { writeUrl: false });
  },

  showIflow({ iflow, level = '' }) {
    this.q.iflow = iflow;
    this.q.level = level;
    this.filtersChanged();
  },

  onPageShown(page) {
    if (page !== 'browse') return;
    const revision = this._filterRevision;
    queueMicrotask(() => {
      if (revision !== this._filterRevision) return;
      try {
        const state = parseBrowseHash(window.location.hash);
        if (!state) return;
        this.q = state.filters;
        this.currentPage = state.page;
        this.selected = null;
        this.ensureSearchWindow();
        if (!validRange(this.q)) throw new TypeError('Invalid Browse URL: date_from must not be after date_to');
        this.writeUrl(state.page, state.entry, true);
        this.loadIflows();
        this.search(state.page, { restoreEntry: state.entry, writeUrl: false });
      } catch (error) {
        this.setUrlError(error);
      }
    });
  },

  closeRow() {
    if (!this.selected) return;
    this.selected = null;
    this.writeUrl(this.currentPage, null);
  },

  async toggleRow(row) {
    if (this.selected?.id === row.id) {
      this.selected = null;
      this.writeUrl(this.currentPage, null);
      return;
    }
    this.selected = row;
    this.writeUrl(this.currentPage, row.id);
    await this.openRow(row);
  },

  async openRow(row) {
    this.selected = row;
    if (row.raw_line === undefined) {
      try {
        row.raw_line = (await api.logs.get(row.id)).raw_line;
      } catch (error) {
        this.$store.toast.notify(`Couldn't load log entry: ${error.message}`, 'error');
      }
    }
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
