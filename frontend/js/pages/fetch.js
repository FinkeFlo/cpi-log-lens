// Fetch page: the fetch form (with its saved default), progress of the job, and schedules.
import { api } from '../api.js';
import { LOGS_CHANGED, OPEN_TENANT_MODAL, emit } from '../events.js';
import { describeError } from '../states.js';

const LOG_TYPES = ['trace', 'http'];

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
    form: { id: '', name: '', tenants: [], log_types: [...LOG_TYPES], hours: 1, interval_minutes: 15, enabled: true },
  },
  confirmDeleteScheduleId: null,

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
  async loadSchedules() {
    try {
      this.schedules = await api.schedules.list();
      this.schedulesLoaded = true;
      this.schedulesError = null;
    } catch (e) {
      this.schedulesError = describeError(e, 'schedules');
    }
  },

  openScheduleModal(schedule = null) {
    this.scheduleModal.editing = !!schedule;
    this.scheduleModal.form = schedule
      ? { id: schedule.id, name: schedule.name,
          tenants: schedule.tenants.includes('all') ? this.tenants.map(t => t.id) : [...schedule.tenants],
          log_types: [...schedule.log_types], hours: schedule.hours,
          interval_minutes: schedule.interval_minutes, enabled: schedule.enabled }
      : { id: '', name: '', tenants: this.tenants.map(t => t.id), log_types: [...LOG_TYPES],
          hours: 1, interval_minutes: 15, enabled: true };
    this.scheduleModal.open = true;
  },

  async saveSchedule() {
    const f = this.scheduleModal.form;
    if (!f.name.trim() || f.tenants.length === 0 || f.log_types.length === 0) {
      this.$store.toast.notify('Enter a name and select at least one tenant and log type.', 'error');
      return;
    }
    const body = {
      name: f.name.trim(),
      tenants: this.tenantSelection(f.tenants),
      log_types: f.log_types,
      hours: parseInt(f.hours),
      interval_minutes: parseInt(f.interval_minutes),
      enabled: !!f.enabled,
    };
    try {
      await api.schedules.save(this.scheduleModal.editing ? f.id : null, body);
      this.scheduleModal.open = false;
      await this.loadSchedules();
      this.$store.toast.notify('Schedule saved', 'success');
    } catch (e) {
      this.$store.toast.notify(`Couldn't save schedule: ${e.message}`, 'error');
    }
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

  scheduleTenantLabel(s) {
    if (s.tenants.includes('all')) return 'All tenants';
    return s.tenants.map(id => this.$store.tenants.name(id)).join(', ');
  },
});
