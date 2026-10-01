/* CPI Log Lens — App Logic (Alpine.js) */

/** Error answers carry {"detail": message} or, for invalid input (422), a list of fields. */
function errorMessage(data, res) {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(e => `${(e.loc || []).filter(x => x !== 'body').join('.')}: ${e.msg}`).join('; ');
  }
  return `HTTP ${res.status}`;
}

/** Call the API and return the JSON answer; non-2xx answers throw an Error with the server's message. */
async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(errorMessage(data, res));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function App() {
  return {
    // ── State ────────────────────────────────────────────────────────────────
    page: 'browse',
    loading: false,
    tenants: [],
    logs: { items: [], total: 0, page: 1, pages: 1, page_size: 100 },
    selected: null,
    stats: {},
    iflowStats: [],
    statsFilter: '',
    statsLoading: false,
    dbInfo: {},
    confirmClearOpen: false,
    confirmCleanupOpen: false,
    cleanup: { tenant: 'all', olderThanDays: 30, busy: false },
    clearDbBusy: false,

    // Query params for Browse
    q: { tenant: '', level: '', iflow: '', grep: '', date_from: '', date_to: '' },

    // Fetch form + background job state
    fetch: {
      tenants: [],      // array of selected tenant ids
      log_types: ['trace', 'http'],
      hours: 24,
      // job state (updated live from SSE)
      jobId:       null,
      status:      'idle',   // idle | running | done | error
      statusMsg:   '',
      done:        0,
      total:       0,
      currentFile: '',
      currentTenant: '',
      imported:    0,
      errorMsg:    '',
    },

    // Fetch schedules (recurring pulls) + default fetch config
    schedules: [],
    scheduleModal: {
      open: false,
      editing: false,
      form: { id: '', name: '', tenants: [], log_types: ['trace', 'http'], hours: 1, interval_minutes: 15, enabled: true },
    },
    confirmDeleteScheduleId: null,

    // active SSE connection for fetch progress
    _fetchEventSource: null,

    // Tenant modal
    tenantModal: {
      open: false,
      editing: false,
      form: { id: '', name: '', api_url: '', oauth_url: '', client_id: '', client_secret: '' },
      serviceKey: '',
      serviceKeyError: '',
    },
    tenantsLoaded: false,
    demoStarting: false,

    // Toast
    toast: { show: false, msg: '', type: 'info' },

    // Charts (Chart.js instances)
    _charts: {},

    // ── Init ─────────────────────────────────────────────────────────────────
    async init() {
      // Read initial page from URL hash
      this._applyHash();
      // Keep page in sync when user presses back/forward
      window.addEventListener('hashchange', () => this._applyHash());

      await this.loadDefaultFetchConfig();
      await this.loadTenants();
      await this.loadSchedules();
      await this.search();
      await this.loadDbInfo();
      await this._reconnectFetchStream();
    },

    _applyHash() {
      const valid = ['browse', 'fetch', 'stats', 'settings'];
      // Split "#browse?tenant=x&level=ERROR" into page and query string
      const raw   = window.location.hash.slice(1); // remove leading #
      const [pagePart, queryPart] = raw.split('?');
      const page  = valid.includes(pagePart) ? pagePart : 'browse';

      if (this.page !== page) {
        this.page = page;
        if (page === 'stats')    this.loadStats();
        if (page === 'settings') this.loadDbInfo();
      }

      // Restore filters when navigating to browse via URL
      if (page === 'browse' && queryPart !== undefined) {
        const p = new URLSearchParams(queryPart);
        this.q.tenant    = p.get('tenant')    || '';
        this.q.level     = p.get('level')     || '';
        this.q.iflow     = p.get('iflow')     || '';
        this.q.grep      = p.get('grep')      || '';
        this.q.date_from = p.get('date_from') || '';
        this.q.date_to   = p.get('date_to')   || '';
      }
    },

    _pushHash(page = this.page) {
      // Build hash — include filter params only on browse page
      let hash = page;
      if (page === 'browse') {
        const p = new URLSearchParams();
        if (this.q.tenant)    p.set('tenant',    this.q.tenant);
        if (this.q.level)     p.set('level',     this.q.level);
        if (this.q.iflow)     p.set('iflow',     this.q.iflow);
        if (this.q.grep)      p.set('grep',      this.q.grep);
        if (this.q.date_from) p.set('date_from', this.q.date_from);
        if (this.q.date_to)   p.set('date_to',   this.q.date_to);
        const qs = p.toString();
        if (qs) hash += '?' + qs;
      }
      // replaceState keeps the URL in sync without firing hashchange
      history.replaceState(null, '', '#' + hash);
    },

    async navigate(target) {
      this.page = target;
      this._pushHash(target);
      if (target === 'stats')    await this.loadStats();
      if (target === 'settings') await this.loadDbInfo();
    },

    // ── Toast ─────────────────────────────────────────────────────────────────
    notify(msg, type = 'info') {
      this.toast = { show: true, msg, type };
      setTimeout(() => { this.toast.show = false; }, 3500);
    },

    // ── Tenants ───────────────────────────────────────────────────────────────
    async loadTenants() {
      try {
        this.tenants = await api('/api/tenants');
        this.tenantsLoaded = true;
        if (this._wantAllTenants) {
          // Saved default config was ["all"] — resolve to the concrete list
          // now that tenants are known.
          this.fetch.tenants = this.tenants.map(t => t.id);
        } else if (this.tenants.length > 0 && this.fetch.tenants.length === 0 && !this._hasDefaultFetchConfig) {
          // Only default to "all tenants" if no saved default config already
          // populated the selection (see loadDefaultFetchConfig).
          this.fetch.tenants = this.tenants.map(t => t.id);
        }
      } catch (e) {
        console.error('loadTenants:', e);
      }
    },

    // ── Fetch: default form config ─────────────────────────────────────────────
    async loadDefaultFetchConfig() {
      try {
        const cfg = await api('/api/fetch/default-config');
        if (cfg) {
          this._hasDefaultFetchConfig = true;
          this.fetch.tenants   = cfg.tenants.includes('all') ? [] : cfg.tenants; // resolved against tenants once loaded below
          this.fetch.log_types = cfg.log_types;
          this.fetch.hours     = cfg.hours;
          if (cfg.tenants.includes('all')) {
            // Resolved once tenants are loaded (loadTenants runs right after this)
            this._wantAllTenants = true;
          }
        }
      } catch (e) {
        console.error('loadDefaultFetchConfig:', e);
      }
    },

    async saveDefaultFetchConfig() {
      try {
        const body = {
          tenants:   this.fetch.tenants.length === this.tenants.length ? ['all'] : this.fetch.tenants,
          log_types: this.fetch.log_types,
          hours:     parseInt(this.fetch.hours),
        };
        await api('/api/fetch/default-config', { method: 'PUT', body });
        this._hasDefaultFetchConfig = true;
        this.notify('Saved as default', 'success');
      } catch (e) {
        this.notify(`Couldn't save default: ${e.message}`, 'error');
      }
    },

    // ── Fetch schedules (recurring pulls) ───────────────────────────────────────
    async loadSchedules() {
      try {
        this.schedules = await api('/api/schedules');
      } catch (e) {
        console.error('loadSchedules:', e);
      }
    },

    openScheduleModal(schedule = null) {
      this.scheduleModal.editing = !!schedule;
      this.scheduleModal.form = schedule
        ? { id: schedule.id, name: schedule.name,
            tenants: schedule.tenants.includes('all') ? this.tenants.map(t => t.id) : [...schedule.tenants],
            log_types: [...schedule.log_types], hours: schedule.hours,
            interval_minutes: schedule.interval_minutes, enabled: schedule.enabled }
        : { id: '', name: '', tenants: this.tenants.map(t => t.id), log_types: ['trace', 'http'],
            hours: 1, interval_minutes: 15, enabled: true };
      this.scheduleModal.open = true;
    },

    async saveSchedule() {
      const f = this.scheduleModal.form;
      if (!f.name.trim() || f.tenants.length === 0 || f.log_types.length === 0) {
        this.notify('Enter a name and select at least one tenant and log type.', 'error');
        return;
      }
      const body = {
        name:             f.name.trim(),
        tenants:          f.tenants.length === this.tenants.length ? ['all'] : f.tenants,
        log_types:        f.log_types,
        hours:            parseInt(f.hours),
        interval_minutes: parseInt(f.interval_minutes),
        enabled:          !!f.enabled,
      };
      const method = this.scheduleModal.editing ? 'PUT' : 'POST';
      const url = this.scheduleModal.editing ? `/api/schedules/${f.id}` : '/api/schedules';
      try {
        await api(url, { method, body });
        this.scheduleModal.open = false;
        await this.loadSchedules();
        this.notify('Schedule saved', 'success');
      } catch (e) {
        this.notify(`Couldn't save schedule: ${e.message}`, 'error');
      }
    },

    async toggleSchedule(s) {
      try {
        const body = {
          name: s.name, tenants: s.tenants, log_types: s.log_types,
          hours: s.hours, interval_minutes: s.interval_minutes, enabled: !s.enabled,
        };
        await api(`/api/schedules/${s.id}`, { method: 'PUT', body });
        await this.loadSchedules();
      } catch (e) {
        this.notify(`Couldn't update schedule: ${e.message}`, 'error');
      }
    },

    async deleteSchedule(id) {
      this.confirmDeleteScheduleId = null;
      try {
        await api(`/api/schedules/${id}`, { method: 'DELETE' });
        this.notify('Schedule deleted');
      } catch (e) {
        this.notify(`Couldn't delete the schedule: ${e.message}`, 'error');
      }
      await this.loadSchedules();
    },

    scheduleTenantLabel(s) {
      if (s.tenants.includes('all')) return 'All tenants';
      return s.tenants
        .map(id => this.tenants.find(t => t.id === id)?.name || id)
        .join(', ');
    },

    async toggleRow(row) {
      if (this.selected?.id === row.id) { this.selected = null; return; }
      this.selected = row;
      // The list omits raw_line; load it once when the row is opened.
      if (row.raw_line === undefined) {
        try {
          row.raw_line = (await api(`/api/logs/${row.id}`)).raw_line;
        } catch (e) {
          row.raw_line = null;
        }
      }
    },

    // Fill the tenant form from a CPI service key (BTP cockpit → service
    // instance → service key), which users usually have as JSON.
    applyServiceKey() {
      const m = this.tenantModal;
      if (!m.serviceKey.trim()) { m.serviceKeyError = ''; return; }
      try {
        const key = JSON.parse(m.serviceKey);
        const o = key.oauth || key;
        if (!o.url || !o.tokenurl || !o.clientid || !o.clientsecret) throw new Error('missing fields');
        m.form.api_url = o.url.replace(/\/+$/, '');
        m.form.oauth_url = o.tokenurl;
        m.form.client_id = o.clientid;
        m.form.client_secret = o.clientsecret;
        m.serviceKeyError = '';
      } catch (e) {
        m.serviceKeyError = 'Not a CPI service key: expected JSON with url, tokenurl, clientid and clientsecret.';
      }
    },

    async startDemo() {
      this.demoStarting = true;
      try {
        const data = await api('/api/demo', { method: 'POST' });
        await this.loadTenants();
        this.fetch.status = 'running';
        this.fetch.jobId = data.job_id;
        this._openFetchStream();
        this.notify('Importing demo data…', 'success');
      } catch (e) {
        this.notify(`Couldn't start the demo: ${e.message}`, 'error');
      } finally {
        this.demoStarting = false;
      }
    },

    openTenantModal(tenant = null) {
      this.tenantModal.editing = !!tenant;
      this.tenantModal.form = tenant
        ? { ...tenant, client_secret: '' }
        : { id: '', name: '', api_url: '', oauth_url: '', client_id: '', client_secret: '' };
      this.tenantModal.serviceKey = '';
      this.tenantModal.serviceKeyError = '';
      this.tenantModal.open = true;
    },

    async saveTenant() {
      const f = this.tenantModal.form;
      const method = this.tenantModal.editing ? 'PUT' : 'POST';
      const url = this.tenantModal.editing ? `/api/tenants/${f.id}` : '/api/tenants';
      try {
        await api(url, { method, body: f });
        this.tenantModal.open = false;
        await this.loadTenants();
        this.notify('Tenant saved', 'success');
      } catch (e) {
        this.notify(`Couldn't save tenant: ${e.message}`, 'error');
      }
    },

    async testTenant(t) {
      try {
        await api(`/api/tenants/${t.id}/test`, { method: 'POST' });
        t._testOk = true;
        t._testResult = '✓ Connection successful';
      } catch (e) {
        t._testOk = false;
        t._testResult = `✗ Connection failed: ${e.message}`;
      }
    },

    async deleteTenant(id) {
      if (!confirm(`Delete tenant "${id}"? Scheduled fetches no longer include it.`)) return;
      const purge = confirm(
        `Also delete the imported log entries and downloaded files of "${id}"?\n\n` +
        'OK: delete them. Cancel: keep them (a tenant added again with this ID continues where it stopped).'
      );
      let data;
      try {
        data = await api(`/api/tenants/${encodeURIComponent(id)}?purge=${purge}`, { method: 'DELETE' });
      } catch (e) {
        this.notify(`Couldn't delete the tenant: ${e.message}`, 'error');
        return;
      } finally {
        await this.loadTenants();
        await this.loadSchedules();
      }
      if (purge) {
        await this.loadDbInfo();
        await this.search();
        this.notify(`Tenant deleted with ${data.deleted_entries.toLocaleString('en-US')} entries`);
      } else {
        this.notify('Tenant deleted, its log entries are kept');
      }
    },

    // ── Browse ────────────────────────────────────────────────────────────────
    async search(page = 1) {
      this.loading = true;
      this.selected = null;
      try {
        const params = new URLSearchParams({ page, page_size: 100 });
        if (this.q.tenant)    params.set('tenant',    this.q.tenant);
        if (this.q.level)     params.set('level',     this.q.level);
        if (this.q.iflow)     params.set('iflow',     this.q.iflow);
        if (this.q.grep)      params.set('grep',      this.q.grep);
        if (this.q.date_from) params.set('date_from', this.q.date_from);
        if (this.q.date_to)   params.set('date_to',   this.q.date_to);
        this.logs = await api(`/api/logs?${params}`);
        // Only sync URL on page 1 — pagination is ephemeral
        if (page === 1) this._pushHash();
      } catch (e) {
        this.notify(`Couldn't load logs: ${e.message}`, 'error');
      } finally {
        this.loading = false;
      }
    },

    resetSearch() {
      this.q = { tenant: '', level: '', iflow: '', grep: '', date_from: '', date_to: '' };
      this.search();
    },

    goPage(p) {
      this.search(p);
    },

    pageRange() {
      const { page, pages } = this.logs;
      const delta = 2;
      const range = [];
      for (let i = Math.max(1, page - delta); i <= Math.min(pages, page + delta); i++) {
        range.push(i);
      }
      return range;
    },

    shortIflow(iflow) {
      if (!iflow) return '';
      // Strip "Camel (NAME) thread #N" wrapper from existing DB data
      const camel = iflow.match(/Camel \(([^)]+)\)/);
      if (camel) return camel[1];
      // Strip "scheduler-NAME_Worker-N" and "12345-NAME_Worker-N"
      const sched = iflow.match(/^(?:scheduler-|\d+-?)(.+?)(?:_Worker.*)?$/);
      if (sched) iflow = sched[1];
      // Show short form: prefer IF_XXXX segment
      const parts = iflow.split('-');
      for (const p of parts) {
        if (p.startsWith('IF_')) return iflow; // show full name once we found IF_
      }
      return iflow.length > 40 ? iflow.slice(-38) + '…' : iflow;
    },

    // ── Fetch: start background job ───────────────────────────────────────────
    async startFetch() {
      if (this.fetch.status === 'running') return;

      this.fetch.status      = 'running';
      this.fetch.statusMsg   = 'Connecting…';
      this.fetch.done        = 0;
      this.fetch.total       = 0;
      this.fetch.currentFile = '';
      this.fetch.imported    = 0;
      this.fetch.errorMsg    = '';

      try {
        const data = await api('/api/fetch', {
          method: 'POST',
          body: {
            tenants:   this.fetch.tenants.length === this.tenants.length ? ['all'] : this.fetch.tenants,
            log_types: this.fetch.log_types,
            hours:     parseInt(this.fetch.hours),
          },
        });
        this.fetch.jobId = data.job_id;
        this._openFetchStream();
      } catch (e) {
        this.fetch.status   = 'error';
        this.fetch.errorMsg = e.message;
      }
    },

    /** Open/reopen the SSE stream for the active job. */
    _openFetchStream() {
      if (this._fetchEventSource) {
        this._fetchEventSource.close();
        this._fetchEventSource = null;
      }

      const es = new EventSource('/api/fetch/stream');
      this._fetchEventSource = es;

      es.onmessage = (e) => {
        const event = JSON.parse(e.data);
        this._handleFetchEvent(event);
      };

      es.onerror = () => {
        // SSE closed (server done or network hiccup) — stop listening
        es.close();
        this._fetchEventSource = null;
      };
    },

    /** Called on page load to reconnect to any still-running job. */
    async _reconnectFetchStream() {
      try {
        const data = await api('/api/fetch/status');
        if (data.status === 'idle') return;

        // Restore state from snapshot
        this.fetch.jobId       = data.job_id;
        this.fetch.status      = data.status;
        this.fetch.statusMsg   = data.status_msg;
        this.fetch.done        = data.done;
        this.fetch.total       = data.total;
        this.fetch.currentFile = data.current_file;
        this.fetch.currentTenant = data.current_tenant;
        this.fetch.imported    = data.imported;
        this.fetch.errorMsg    = data.error_msg;

        if (data.status === 'running') {
          this._openFetchStream();
        }
      } catch (e) {
        console.error('_reconnectFetchStream:', e);
      }
    },

    _handleFetchEvent(ev) {
      switch (ev.type) {
        case 'snapshot':
          this.fetch.jobId       = ev.job_id;
          this.fetch.status      = ev.status;
          this.fetch.statusMsg   = ev.status_msg;
          this.fetch.done        = ev.done;
          this.fetch.total       = ev.total;
          this.fetch.currentFile = ev.current_file;
          this.fetch.currentTenant = ev.current_tenant;
          this.fetch.imported    = ev.imported;
          this.fetch.errorMsg    = ev.error_msg;
          break;
        case 'status':
          this.fetch.statusMsg = ev.msg;
          break;
        case 'files_found':
          this.fetch.total     = ev.count;
          this.fetch.currentTenant = ev.tenant;
          this.fetch.statusMsg = `${ev.tenant} · ${ev.log_type}: ${ev.count} files found`;
          break;
        case 'progress':
          this.fetch.done        = ev.done;
          this.fetch.total       = ev.total;
          this.fetch.currentFile = ev.file;
          this.fetch.imported    = ev.imported;
          // Auto-refresh Browse list when new rows were imported
          if (ev.new_rows > 0 && this.page === 'browse') {
            this.search(this.logs.page);
          }
          break;
        case 'warn':
          console.warn(ev.msg);
          break;
        case 'error':
          this.fetch.status   = 'error';
          this.fetch.errorMsg = ev.msg;
          if (this._fetchEventSource) {
            this._fetchEventSource.close();
            this._fetchEventSource = null;
          }
          break;
        case 'done':
          this.fetch.status      = 'done';
          this.fetch.imported    = ev.imported;
          this.fetch.statusMsg   = 'Completed';
          if (this._fetchEventSource) {
            this._fetchEventSource.close();
            this._fetchEventSource = null;
          }
          // Final refresh of Browse list
          if (this.page === 'browse') this.search(1);
          break;
        case 'cancelled':
          this.fetch.status      = 'cancelled';
          this.fetch.imported    = ev.imported;
          this.fetch.statusMsg   = 'Cancelled';
          if (this._fetchEventSource) {
            this._fetchEventSource.close();
            this._fetchEventSource = null;
          }
          if (this.page === 'browse') this.search(1);
          break;
      }
    },

    /** Request cancellation of the currently running fetch job. */
    async cancelFetch() {
      if (this.fetch.status !== 'running') return;
      this.fetch.statusMsg = 'Cancelling…';
      try {
        await api('/api/fetch/cancel', { method: 'POST' });
      } catch (e) {
        console.error('cancelFetch:', e);
      }
    },

    // ── Stats ─────────────────────────────────────────────────────────────────
    async loadStats() {
      this.statsLoading = true;
      try {
        const params = this.statsFilter ? `?tenant=${this.statsFilter}` : '';
        this.stats = await api(`/api/stats${params}`);

        // Aggregate iflow stats from top_errors + all logs
        const map = {};
        for (const row of (this.stats.top_errors || [])) {
          if (!row.iflow) continue;
          const key = this.shortIflow(row.iflow);
          if (!map[key]) map[key] = { iflow: key, error: 0, warn: 0, info: 0, total: 0 };
          map[key].error += row.cnt || 0;
          map[key].total += row.cnt || 0;
        }
        this.iflowStats = Object.values(map).sort((a, b) => b.error - a.error);

        this.$nextTick(() => this.renderCharts());
      } catch (e) {
        this.notify(`Couldn't load statistics: ${e.message}`, 'error');
      } finally {
        this.statsLoading = false;
      }
    },

    renderCharts() {
      const gridColor = 'rgba(255,255,255,0.07)';
      const textColor = 'rgba(255,255,255,0.45)';

      const mkChart = (id, config) => {
        const canvas = document.getElementById(id);
        if (!canvas) return;
        if (this._charts[id]) { this._charts[id].destroy(); }
        this._charts[id] = new Chart(canvas, config);
      };

      // Top errors — horizontal bar
      const errLabels = (this.stats.top_errors || []).slice(0, 10).map(r => this.shortIflow(r.iflow));
      const errData   = (this.stats.top_errors || []).slice(0, 10).map(r => r.cnt);
      mkChart('chartErrors', {
        type: 'bar',
        data: {
          labels: errLabels,
          datasets: [{ label: 'Errors', data: errData, backgroundColor: 'rgba(248,113,113,0.8)', borderRadius: 3 }],
        },
        options: {
          indexAxis: 'y', responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { color: gridColor }, ticks: { color: textColor } },
            y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 10 } } },
          },
        },
      });

      // Level doughnut
      const levels = this.stats.levels || [];
      mkChart('chartLevels', {
        type: 'doughnut',
        data: {
          labels: levels.map(l => l.lvl || 'OTHER'),
          datasets: [{
            data: levels.map(l => l.cnt),
            backgroundColor: levels.map(l =>
              l.lvl === 'ERROR' ? '#f87171' : l.lvl === 'WARN' ? '#fbbf24' : l.lvl === 'INFO' ? '#34d399' : '#94a3b8'
            ),
          }],
        },
        options: {
          responsive: true,
          plugins: { legend: { position: 'right', labels: { color: textColor, boxWidth: 12 } } },
        },
      });

      // Timeline
      const tl = this.stats.timeline || [];
      mkChart('chartTimeline', {
        type: 'line',
        data: {
          labels: tl.map(r => r.hour?.slice(11) || r.hour),
          datasets: [{
            label: 'Errors per hour', data: tl.map(r => r.cnt),
            borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.12)',
            fill: true, tension: 0.4, pointRadius: 0,
          }],
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { color: gridColor }, ticks: { color: textColor, maxTicksLimit: 12 } },
            y: { grid: { color: gridColor }, ticks: { color: textColor } },
          },
        },
      });
    },

    // ── DB ────────────────────────────────────────────────────────────────────
    async loadDbInfo() {
      try {
        this.dbInfo = await api('/api/db/info');
      } catch (e) {
        console.error(e);
      }
    },

    async clearDb() {
      this.clearDbBusy = true;
      try {
        await api('/api/db/clear', { method: 'POST' });
        this.confirmClearOpen = false;
        await this.loadDbInfo();
        await this.search();
        this.notify('Database cleared');
      } catch (e) {
        this.notify(`Couldn't clear the database: ${e.message}`, 'error');
      } finally {
        this.clearDbBusy = false;
      }
    },

    async cleanupLogs() {
      this.cleanup.busy = true;
      try {
        const data = await api('/api/db/cleanup', {
          method: 'POST',
          body: { older_than_days: this.cleanup.olderThanDays, tenant: this.cleanup.tenant },
        });
        this.confirmCleanupOpen = false;
        await this.loadDbInfo();
        await this.search();
        this.notify(`Deleted ${data.deleted.toLocaleString('en-US')} entries (${data.remaining.toLocaleString('en-US')} remaining)`);
      } catch (e) {
        this.confirmCleanupOpen = false;
        this.notify(`Couldn't delete old entries: ${e.message}`, 'error');
      } finally {
        this.cleanup.busy = false;
      }
    },
  };
}
