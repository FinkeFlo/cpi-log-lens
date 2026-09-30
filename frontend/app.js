/* CPI Log Explorer — App Logic (Alpine.js) */

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
    },

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
        const res = await fetch('/api/tenants');
        this.tenants = await res.json();
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
        const res = await fetch('/api/fetch/default-config');
        const cfg = await res.json();
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
        const res = await fetch('/api/fetch/default-config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        this._hasDefaultFetchConfig = true;
        this.notify('Als Standard gespeichert', 'success');
      } catch (e) {
        this.notify(`Fehler: ${e.message}`, 'error');
      }
    },

    // ── Fetch schedules (recurring pulls) ───────────────────────────────────────
    async loadSchedules() {
      try {
        const res = await fetch('/api/schedules');
        this.schedules = await res.json();
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
        this.notify('Bitte Name, Tenant(s) und Log-Typ(en) angeben', 'error');
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
        const res = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        this.scheduleModal.open = false;
        await this.loadSchedules();
        this.notify('Zeitplan gespeichert', 'success');
      } catch (e) {
        this.notify(`Fehler: ${e.message}`, 'error');
      }
    },

    async toggleSchedule(s) {
      try {
        const body = {
          name: s.name, tenants: s.tenants, log_types: s.log_types,
          hours: s.hours, interval_minutes: s.interval_minutes, enabled: !s.enabled,
        };
        const res = await fetch(`/api/schedules/${s.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        await this.loadSchedules();
      } catch (e) {
        this.notify(`Fehler: ${e.message}`, 'error');
      }
    },

    async deleteSchedule(id) {
      await fetch(`/api/schedules/${id}`, { method: 'DELETE' });
      this.confirmDeleteScheduleId = null;
      await this.loadSchedules();
      this.notify('Zeitplan gelöscht');
    },

    scheduleTenantLabel(s) {
      if (s.tenants.includes('all')) return 'Alle Tenants';
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
          const res = await fetch(`/api/logs/${row.id}`);
          row.raw_line = res.ok ? (await res.json()).raw_line : null;
        } catch (e) {
          row.raw_line = null;
        }
      }
    },

    openTenantModal(tenant = null) {
      this.tenantModal.editing = !!tenant;
      this.tenantModal.form = tenant
        ? { ...tenant, client_secret: '' }
        : { id: '', name: '', api_url: '', oauth_url: '', client_id: '', client_secret: '' };
      this.tenantModal.open = true;
    },

    async saveTenant() {
      const f = this.tenantModal.form;
      const method = this.tenantModal.editing ? 'PUT' : 'POST';
      const url = this.tenantModal.editing ? `/api/tenants/${f.id}` : '/api/tenants';
      try {
        const res = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(f),
        });
        if (!res.ok) throw new Error(await res.text());
        this.tenantModal.open = false;
        await this.loadTenants();
        this.notify('Tenant gespeichert', 'success');
      } catch (e) {
        this.notify(`Fehler: ${e.message}`, 'error');
      }
    },

    async testTenant(t) {
      try {
        const res = await fetch(`/api/tenants/${t.id}/test`, { method: 'POST' });
        const data = await res.json();
        t._testOk = data.ok;
        t._testResult = data.ok ? '✓ Verbindung erfolgreich' : `✗ ${data.error}`;
      } catch (e) {
        t._testOk = false;
        t._testResult = `✗ ${e.message}`;
      }
    },

    async deleteTenant(id) {
      if (!confirm(`Tenant "${id}" wirklich löschen?`)) return;
      const res = await fetch(`/api/tenants/${id}`, { method: 'DELETE' });
      await this.loadTenants();
      if (res.ok) this.notify('Tenant gelöscht');
      else this.notify(`Could not delete tenant (HTTP ${res.status})`, 'error');
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
        const res = await fetch(`/api/logs?${params}`);
        this.logs = await res.json();
        // Only sync URL on page 1 — pagination is ephemeral
        if (page === 1) this._pushHash();
      } catch (e) {
        this.notify(`Fehler beim Laden: ${e.message}`, 'error');
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
      this.fetch.statusMsg   = 'Verbindung wird hergestellt…';
      this.fetch.done        = 0;
      this.fetch.total       = 0;
      this.fetch.currentFile = '';
      this.fetch.imported    = 0;
      this.fetch.errorMsg    = '';

      try {
        const res = await fetch('/api/fetch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tenants:   this.fetch.tenants.length === this.tenants.length ? ['all'] : this.fetch.tenants,
            log_types: this.fetch.log_types,
            hours:     parseInt(this.fetch.hours),
          }),
        });

        const data = await res.json();
        if (!data.ok) {
          this.fetch.status   = 'error';
          this.fetch.errorMsg = data.error || 'Unbekannter Fehler';
          return;
        }

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
        const res = await fetch('/api/fetch/status');
        const data = await res.json();
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
          this.fetch.statusMsg = `${ev.tenant} / ${ev.log_type}: ${ev.count} Dateien gefunden`;
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
          this.fetch.statusMsg   = 'Abgeschlossen';
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
          this.fetch.statusMsg   = 'Abgebrochen';
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
      this.fetch.statusMsg = 'Abbruch angefordert …';
      try {
        await fetch('/api/fetch/cancel', { method: 'POST' });
      } catch (e) {
        console.error('cancelFetch:', e);
      }
    },

    // ── Stats ─────────────────────────────────────────────────────────────────
    async loadStats() {
      this.statsLoading = true;
      try {
        const params = this.statsFilter ? `?tenant=${this.statsFilter}` : '';
        const res = await fetch(`/api/stats${params}`);
        this.stats = await res.json();

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
        this.notify(`Stats-Fehler: ${e.message}`, 'error');
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
          datasets: [{ label: 'Fehler', data: errData, backgroundColor: 'rgba(248,113,113,0.8)', borderRadius: 3 }],
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
            label: 'Fehler/h', data: tl.map(r => r.cnt),
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
        const res = await fetch('/api/db/info');
        this.dbInfo = await res.json();
      } catch (e) {
        console.error(e);
      }
    },

    async clearDb() {
      this.clearDbBusy = true;
      try {
        await fetch('/api/db/clear', { method: 'POST' });
        this.confirmClearOpen = false;
        await this.loadDbInfo();
        await this.search();
        this.notify('Datenbank geleert');
      } catch (e) {
        console.error('clearDb:', e);
        this.notify('Leeren fehlgeschlagen', 'error');
      } finally {
        this.clearDbBusy = false;
      }
    },

    async cleanupLogs() {
      this.cleanup.busy = true;
      try {
        const res = await fetch('/api/db/cleanup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            older_than_days: this.cleanup.olderThanDays,
            tenant: this.cleanup.tenant,
          }),
        });
        const data = await res.json();
        this.confirmCleanupOpen = false;
        if (!res.ok || !data.ok) {
          this.notify(data.detail || 'Bereinigung fehlgeschlagen', 'error');
          return;
        }
        await this.loadDbInfo();
        await this.search();
        this.notify(`${data.deleted.toLocaleString()} Einträge gelöscht (${data.remaining.toLocaleString()} verbleiben)`);
      } catch (e) {
        console.error('cleanupLogs:', e);
        this.notify('Bereinigung fehlgeschlagen', 'error');
      } finally {
        this.cleanup.busy = false;
      }
    },
  };
}
