// Stats page: totals, charts (Chart.js, loaded as a global script) and the IFlow table.
import { api } from '../api.js';
import { BROWSE_IFLOW, emit } from '../events.js';
import { shortIflow } from '../format.js';

// Chart.js instances, outside Alpine's reactivity.
const charts = {};

export default () => ({
  stats: {},
  iflowStats: [],
  statsFilter: '',
  statsLoading: false,

  get tenants() {
    return this.$store.tenants.list;
  },

  init() {
    if (this.$store.route.page === 'stats') this.loadStats();
  },

  onPageShown(page) {
    if (page === 'stats') this.loadStats();
  },

  async loadStats() {
    this.statsLoading = true;
    try {
      this.stats = await api.stats.get(this.statsFilter);

      this.iflowStats = this.stats.iflow_stats || [];

      this.$nextTick(() => this.renderCharts());
    } catch (e) {
      this.$store.toast.notify(`Couldn't load statistics: ${e.message}`, 'error');
    } finally {
      this.statsLoading = false;
    }
  },

  showIflow(iflow) {
    this.$store.route.navigate('browse');
    emit(BROWSE_IFLOW, { iflow, level: 'ERROR' });
  },

  renderCharts() {
    const gridColor = 'rgba(255,255,255,0.07)';
    const textColor = 'rgba(255,255,255,0.45)';

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
        datasets: [{ label: 'Errors', data: topErrors.map(r => r.cnt), backgroundColor: 'rgba(248,113,113,0.8)', borderRadius: 3 }],
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

  shortIflow,
});
