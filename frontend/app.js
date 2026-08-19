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

    // Query params for Browse
    q: { tenant: '', level: '', iflow: '', grep: '', date_from: '', date_to: '' },

    // Fetch form
    fetch: {
      tenant: 'all',
      log_type: 'trace',
      hours: 24,
      status: 'idle',   // idle | running | done | error
      statusMsg: '',
      done: 0,
      total: 0,
      currentFile: '',
      imported: 0,
      errorMsg: '',
    },

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
      await this.loadTenants();
      await this.search();
      await this.loadDbInfo();
    },

    async navigate(target) {
      this.page = target;
      if (target === 'stats') {
        await this.loadStats();
      } else if (target === 'settings') {
        await this.loadDbInfo();
      }
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
        if (this.tenants.length > 0 && !this.fetch.tenant) {
          this.fetch.tenant = 'all';
        }
      } catch (e) {
        console.error('loadTenants:', e);
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
      await fetch(`/api/tenants/${id}`, { method: 'DELETE' });
      await this.loadTenants();
      this.notify('Tenant gelöscht');
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
      const parts = iflow.split('-');
      for (const p of parts) {
        if (p.startsWith('IF_')) return p;
      }
      return iflow.length > 40 ? iflow.slice(-38) + '…' : iflow;
    },

    // ── Fetch (SSE) ───────────────────────────────────────────────────────────
    async startFetch() {
      this.fetch.status     = 'running';
      this.fetch.statusMsg  = 'Verbindung wird hergestellt…';
      this.fetch.done       = 0;
      this.fetch.total      = 0;
      this.fetch.currentFile= '';
      this.fetch.imported   = 0;
      this.fetch.errorMsg   = '';

      try {
        const res = await fetch('/api/fetch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tenant:   this.fetch.tenant,
            log_type: this.fetch.log_type,
            hours:    parseInt(this.fetch.hours),
          }),
        });

        if (!res.ok) {
          const err = await res.text();
          throw new Error(err);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();

          for (const line of lines) {
            if (!line.startsWith('data:')) continue;
            const event = JSON.parse(line.slice(5).trim());
            this._handleFetchEvent(event);
          }
        }
      } catch (e) {
        this.fetch.status   = 'error';
        this.fetch.errorMsg = e.message;
      }
    },

    _handleFetchEvent(ev) {
      switch (ev.type) {
        case 'status':
          this.fetch.statusMsg = ev.msg;
          break;
        case 'files_found':
          this.fetch.total     = ev.count;
          this.fetch.statusMsg = `${ev.tenant} / ${ev.log_type}: ${ev.count} Dateien gefunden`;
          break;
        case 'progress':
          this.fetch.done        = ev.done;
          this.fetch.total       = ev.total;
          this.fetch.currentFile = ev.file;
          break;
        case 'warn':
          console.warn(ev.msg);
          break;
        case 'error':
          this.fetch.status   = 'error';
          this.fetch.errorMsg = ev.msg;
          break;
        case 'done':
          this.fetch.status   = 'done';
          this.fetch.imported = ev.imported;
          this.fetch.statusMsg= 'Abgeschlossen';
          break;
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
      await fetch('/api/db/clear', { method: 'POST' });
      this.confirmClearOpen = false;
      await this.loadDbInfo();
      await this.search();
      this.notify('Datenbank geleert');
    },
  };
}
