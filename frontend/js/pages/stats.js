// Stats page: totals, charts (Chart.js, loaded as a global script) and the IFlow table.
import { api } from '../api.js';
import { BROWSE_IFLOW, emit } from '../events.js';
import { shortIflow } from '../format.js';
import { describeError } from '../states.js';

// Chart.js instances, outside Alpine's reactivity.
const charts = {};

export default () => ({
  stats: {},
  iflowStats: [],
  statsFilter: '',
  statsLoading: false,
  _requestId: 0,
  statsLoaded: false,
  statsError: null, // describeError() of the last failed load

  get tenants() {
    return this.$store.tenants.list;
  },

  // loading | results | empty (no entries at all) | no-matches (none for the tenant) | error
  get statsState() {
    if (this.statsError) return 'error';
    if (!this.statsLoaded) return 'loading';
    if (!this.stats.total) return this.statsFilter ? 'no-matches' : 'empty';
    return 'results';
  },

  init() {
    if (this.$store.route.page === 'stats') this.loadStats();
  },

  onPageShown(page) {
    if (page === 'stats') this.loadStats();
  },

  onThemeChanged() {
    if (this.$store.route.page === 'stats') this.$nextTick(() => this.renderCharts());
  },

  async loadStats() {
    const requestId = ++this._requestId;
    this.statsLoading = true;
    try {
      const stats = await api.stats.get(this.statsFilter);
      if (requestId !== this._requestId) return;
      this.stats = stats;
      this.iflowStats = stats.iflow_stats || [];
      this.statsLoaded = true;
      this.statsError = null;
      this.$nextTick(() => this.renderCharts());
    } catch (e) {
      if (requestId === this._requestId) this.statsError = describeError(e, 'statistics');
    } finally {
      if (requestId === this._requestId) this.statsLoading = false;
    }
  },

  retry() {
    if (this.$store.tenants.error) this.$store.tenants.load();
    this.loadStats();
  },

  showAllTenants() {
    this.statsFilter = '';
    this.loadStats();
  },

  showIflow(iflow) {
    this.$store.route.navigate('browse');
    emit(BROWSE_IFLOW, { iflow, level: 'ERROR' });
  },

  renderCharts() {
    const styles = getComputedStyle(document.documentElement);
    const color = name => styles.getPropertyValue(name).trim();
    const gridColor = color('--lens-chart-grid');
    const textColor = color('--lens-muted');
    const levelColors = {
      ERROR: color('--level-error'),
      WARN: color('--level-warn'),
      INFO: color('--level-info'),
      DEBUG: color('--level-debug'),
    };

    const mkChart = (id, config) => {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      if (charts[id]) charts[id].destroy();
      charts[id] = new window.Chart(canvas, config);
    };

    // Top errors — horizontal bar
    const topErrors = (this.stats.top_errors || []).slice(0, 10);
    mkChart('chartErrors', {
      type: 'bar',
      data: {
        labels: topErrors.map(r => shortIflow(r.iflow)),
        datasets: [{ label: 'Errors', data: topErrors.map(r => r.cnt), backgroundColor: levelColors.ERROR, borderRadius: 3 }],
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
          backgroundColor: levels.map(l => levelColors[l.lvl] || textColor),
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
          borderColor: levelColors.ERROR, backgroundColor: color('--level-error-bg'),
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

  shortIflow,
});
