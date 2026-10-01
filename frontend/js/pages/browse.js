// Browse page: filters (kept in the URL hash), the log list with paging and the entry detail.
import { api } from '../api.js';
import { emit, OPEN_TENANT_MODAL } from '../events.js';
import { shortIflow } from '../format.js';

const FILTERS = ['tenant', 'level', 'iflow', 'grep', 'date_from', 'date_to'];
const emptyQuery = () => Object.fromEntries(FILTERS.map(f => [f, '']));

export default () => ({
  q: emptyQuery(),
  logs: { items: [], total: 0, page: 1, pages: 1, page_size: 100 },
  selected: null,
  loading: false,
  demoStarting: false,

  get tenants() {
    return this.$store.tenants.list;
  },

  init() {
    this.readHash();
    window.addEventListener('hashchange', () => this.readHash());
    this.search();
  },

  // Restore filters from "#browse?tenant=x&level=ERROR" (Back/Forward, links)
  readHash() {
    const [page, query] = window.location.hash.slice(1).split('?');
    if ((page || 'browse') !== 'browse' || query === undefined) return;
    const params = new URLSearchParams(query);
    for (const f of FILTERS) this.q[f] = params.get(f) || '';
  },

  // replaceState keeps the URL in sync without firing hashchange
  pushHash() {
    if (this.$store.route.page !== 'browse') return;
    const params = new URLSearchParams();
    for (const f of FILTERS) if (this.q[f]) params.set(f, this.q[f]);
    const qs = params.toString();
    history.replaceState(null, '', '#browse' + (qs ? '?' + qs : ''));
  },

  async search(page = 1) {
    this.loading = true;
    this.selected = null;
    try {
      this.logs = await api.logs.search({ page, page_size: 100, ...this.q });
      // Only sync URL on page 1 — pagination is ephemeral
      if (page === 1) this.pushHash();
    } catch (e) {
      this.$store.toast.notify(`Couldn't load logs: ${e.message}`, 'error');
    } finally {
      this.loading = false;
    }
  },

  resetSearch() {
    this.q = emptyQuery();
    this.search();
  },

  goPage(p) {
    this.search(p);
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
    this.search(firstPage ? 1 : this.logs.page);
  },

  // Stats page: show the entries of one IFlow
  showIflow(iflow) {
    this.q.iflow = iflow;
    this.search();
  },

  // Shown via the menu or the URL: the URL then carries the current filters
  // (read first, in case the user typed or went back to a Browse URL).
  onPageShown(page) {
    if (page !== 'browse') return;
    this.readHash();
    this.pushHash();
  },

  async toggleRow(row) {
    if (this.selected?.id === row.id) {
      this.selected = null;
      return;
    }
    this.selected = row;
    // The list omits raw_line; load it once when the row is opened.
    if (row.raw_line === undefined) {
      try {
        row.raw_line = (await api.logs.get(row.id)).raw_line;
      } catch (e) {
        row.raw_line = null;
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
