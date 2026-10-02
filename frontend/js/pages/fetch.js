// Fetch page: the fetch form (with its saved default), progress of the job, and schedules.
import { api } from '../api.js';
import { LOGS_CHANGED, OPEN_TENANT_MODAL, emit } from '../events.js';
import {
  INTERVALS, PRESETS, RANGES, coverage, intervalLabel, lastRunSummary, nextRunText, presetLabel, rangeLabel,
  relativeTime, suggestName, withValue,
} from '../schedules.js';
import { describeError } from '../states.js';

const LOG_TYPES = ['trace', 'http'];
// How often the schedule list is read again while the Fetch page is shown (next run, last result).
const SCHEDULES_REFRESH_MS = 30_000;
// A new enabled schedule starts its first run right after saving; then its progress is shown.
const FIRST_RUN_CHECK_MS = 1000;

// No tenant is preselected: a schedule fetches only the tenants chosen for it.
const emptySchedule = () => ({
  id: '', name: '', allTenants: false, tenants: [], log_types: [...LOG_TYPES],
  hours: PRESETS[0].hours, interval_minutes: PRESETS[0].interval_minutes, enabled: true,
});

export default () => ({
  form: {
    tenants: [], // selected tenant ids
    log_types: [...LOG_TYPES],
    hours: 24,
  },
  _hasDefaultConfig: false,
  _wantAllTenants: false,

  schedules: [],
  schedulesLoaded: false,
  schedulesError: null, // describeError() of the last failed load
  scheduleModal: {
    open: false,
    editing: false,
    submitted: false, // a save was tried: show what is missing
    form: emptySchedule(),
  },
  confirmDeleteScheduleId: null,
  startingScheduleId: null, // "Run now" request in flight
  now: Date.now(), // for "in 14 min", refreshed with the list
  presets: PRESETS,

  get job() {
    return this.$store.fetchJob;
  },

  get tenants() {
    return this.$store.tenants.list;
  },

  async init() {
    await this.loadDefaultConfig();
    // Resolve the selection whenever the tenant list is (re)loaded.
    this.$watch('$store.tenants.list', () => this.selectTenants());
    this.selectTenants();
    // A finished fetch may have been a schedule's run: show its result.
    this.$watch('$store.fetchJob.status', (status, previous) => {
      if (previous === 'running' && status !== 'running') this.loadSchedules({ quiet: true });
    });
    setInterval(() => {
      if (this.$store.route.page === 'fetch' && !document.hidden) this.loadSchedules({ quiet: true });
    }, SCHEDULES_REFRESH_MS);
    await this.loadSchedules();
  },

  async loadDefaultConfig() {
    try {
      const cfg = await api.fetch.defaultConfig();
      if (cfg) {
        this._hasDefaultConfig = true;
        // ["all"] is resolved against the tenants once they are loaded
        this.form.tenants = cfg.tenants.includes('all') ? [] : cfg.tenants;
        this.form.log_types = cfg.log_types;
        this.form.hours = cfg.hours;
        this._wantAllTenants = cfg.tenants.includes('all');
      }
    } catch (e) {
      // Unreachable or busy: the tenant list right below shows that already.
      if (e.kind === 'failed') this.$store.toast.notify(`Couldn't load the saved default selection: ${e.message}`, 'error');
    }
  },

  selectTenants() {
    if (!this.$store.tenants.loaded) return;
    const all = this.tenants.map(t => t.id);
    if (this._wantAllTenants) {
      // Saved default config was ["all"] — resolve to the concrete list.
      this.form.tenants = all;
    } else if (all.length > 0 && this.form.tenants.length === 0 && !this._hasDefaultConfig) {
      // Only default to "all tenants" if no saved default config populated the selection.
      this.form.tenants = all;
    }
  },

  // ["all"] when every tenant is selected, so tenants added later are included
  tenantSelection(ids) {
    return ids.length === this.tenants.length ? ['all'] : ids;
  },

  async saveDefaultConfig() {
    try {
      await api.fetch.saveDefaultConfig({
        tenants: this.tenantSelection(this.form.tenants),
        log_types: this.form.log_types,
        hours: parseInt(this.form.hours),
      });
      this._hasDefaultConfig = true;
      this.$store.toast.notify('Saved as default', 'success');
    } catch (e) {
      this.$store.toast.notify(`Couldn't save default: ${e.message}`, 'error');
    }
  },

  startFetch() {
    return this.job.start({
      tenants: this.tenantSelection(this.form.tenants),
      log_types: this.form.log_types,
      hours: parseInt(this.form.hours),
    });
  },

  showLogs() {
    this.$store.route.navigate('browse');
    emit(LOGS_CHANGED, { firstPage: true });
  },

  addTenant() {
    this.$store.route.navigate('settings');
    emit(OPEN_TENANT_MODAL);
  },

  // ── Schedules ──
  // quiet: a background refresh keeps the shown list when it fails.
  async loadSchedules({ quiet = false } = {}) {
    try {
      this.schedules = await api.schedules.list();
      this.schedulesLoaded = true;
      this.schedulesError = null;
    } catch (e) {
      if (!quiet || !this.schedulesLoaded) this.schedulesError = describeError(e, 'schedules');
    }
    this.now = Date.now();
  },

  openScheduleModal(schedule = null) {
    const m = this.scheduleModal;
    m.editing = !!schedule;
    m.submitted = false;
    if (schedule) {
      const all = schedule.tenants.includes('all');
      m.form = {
        id: schedule.id, name: schedule.name, allTenants: all, tenants: all ? [] : [...schedule.tenants],
        log_types: [...schedule.log_types], hours: schedule.hours,
        interval_minutes: schedule.interval_minutes, enabled: schedule.enabled,
      };
    } else {
      m.form = emptySchedule();
    }
    m.open = true;
  },

  // Tenant chips: "All tenants" (also ones added later) or the chosen ones.
  setAllTenants(on) {
    const f = this.scheduleModal.form;
    f.allTenants = on;
    if (on) f.tenants = [];
  },

  toggleScheduleTenant(id, on) {
    const f = this.scheduleModal.form;
    f.allTenants = false;
    f.tenants = on ? [...f.tenants.filter(x => x !== id), id] : f.tenants.filter(x => x !== id);
  },

  applyPreset(p) {
    this.scheduleModal.form.interval_minutes = p.interval_minutes;
    this.scheduleModal.form.hours = p.hours;
  },

  isPreset(p) {
    const f = this.scheduleModal.form;
    return f.interval_minutes === p.interval_minutes && f.hours === p.hours;
  },

  presetLabel,
  intervalLabel,
  rangeLabel,

  get intervalOptions() {
    return withValue(INTERVALS, this.scheduleModal.form.interval_minutes);
  },

  get rangeOptions() {
    return withValue(RANGES, this.scheduleModal.form.hours);
  },

  get scheduleCoverage() {
    const f = this.scheduleModal.form;
    return coverage(f.interval_minutes, f.hours);
  },

  get scheduleErrors() {
    const f = this.scheduleModal.form;
    return {
      tenants: !f.allTenants && f.tenants.length === 0 ? 'Choose at least one tenant.' : '',
      log_types: f.log_types.length === 0 ? 'Choose at least one log type.' : '',
    };
  },

  get suggestedScheduleName() {
    const f = this.scheduleModal.form;
    const names = f.allTenants ? ['All tenants'] : this.tenants.filter(t => f.tenants.includes(t.id)).map(t => t.name);
    return suggestName(names, f.interval_minutes);
  },

  async saveSchedule() {
    const m = this.scheduleModal;
    const f = m.form;
    m.submitted = true;
    if (this.scheduleErrors.tenants || this.scheduleErrors.log_types) return;
    const body = {
      name: f.name.trim() || this.suggestedScheduleName,
      tenants: f.allTenants ? ['all'] : f.tenants,
      log_types: f.log_types,
      hours: Number(f.hours),
      interval_minutes: Number(f.interval_minutes),
      enabled: !!f.enabled,
    };
    let saved;
    try {
      saved = await api.schedules.save(m.editing ? f.id : null, body);
    } catch (e) {
      this.$store.toast.notify(`Couldn't save schedule: ${e.message}`, 'error');
      return;
    }
    m.open = false;
    await this.loadSchedules();
    this.$store.toast.notify('Schedule saved', 'success');
    if (!m.editing && body.enabled) setTimeout(() => this.followFirstRun(saved.id), FIRST_RUN_CHECK_MS);
  },

  // Show the progress of a new schedule's first run, which starts right after saving.
  async followFirstRun(scheduleId) {
    await this.loadSchedules({ quiet: true });
    const run = this.schedules.find(s => s.id === scheduleId)?.last_run;
    if (run?.status === 'running' && this.job.status !== 'running') this.job.follow(run.id);
  },

  async runScheduleNow(s) {
    this.startingScheduleId = s.id;
    try {
      const data = await api.schedules.run(s.id);
      this.job.follow(data.job_id);
      this.$store.toast.notify(`Started "${s.name}"`, 'success');
    } catch (e) {
      if (e.status === 409) {
        this.$store.toast.notify('A fetch is already running. Run the schedule again when it has finished.', 'error');
        if (e.data?.job_id && this.job.status !== 'running') this.job.follow(e.data.job_id);
      } else {
        this.$store.toast.notify(`Couldn't start the schedule: ${e.message}`, 'error');
      }
    } finally {
      this.startingScheduleId = null;
    }
    await this.loadSchedules({ quiet: true });
  },

  async toggleSchedule(s) {
    try {
      await api.schedules.save(s.id, {
        name: s.name, tenants: s.tenants, log_types: s.log_types,
        hours: s.hours, interval_minutes: s.interval_minutes, enabled: !s.enabled,
      });
      await this.loadSchedules();
    } catch (e) {
      this.$store.toast.notify(`Couldn't update schedule: ${e.message}`, 'error');
    }
  },

  async deleteSchedule(id) {
    this.confirmDeleteScheduleId = null;
    try {
      await api.schedules.remove(id);
      this.$store.toast.notify('Schedule deleted');
    } catch (e) {
      this.$store.toast.notify(`Couldn't delete the schedule: ${e.message}`, 'error');
    }
    await this.loadSchedules();
  },

  scheduleTimingLabel(s) {
    return `${intervalLabel(s.interval_minutes)} · ${rangeLabel(s.hours).toLowerCase()} · ${s.log_types.join(', ')}`;
  },

  nextRunLabel(s) {
    return nextRunText(s, this.now);
  },

  lastRun(s) {
    const summary = lastRunSummary(s);
    return { ...summary, ago: relativeTime(summary.at, this.now) };
  },

  localTime(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
    });
  },

  scheduleTenantLabel(s) {
    if (s.tenants.includes('all')) return 'All tenants';
    return s.tenants.map(id => this.$store.tenants.name(id)).join(', ');
  },
});
