// Settings page: tenants (dialog, service key paste, connection test, delete) and the database.
import { api } from '../api.js';
import { LOGS_CHANGED, SCHEDULES_CHANGED, emit } from '../events.js';

const emptyTenant = () => ({ id: '', name: '', api_url: '', oauth_url: '', client_id: '', client_secret: '' });

export default () => ({
  tenantModal: {
    open: false,
    editing: false,
    form: emptyTenant(),
    serviceKey: '',
    serviceKeyError: '',
  },
  dbInfo: {},
  confirmClearOpen: false,
  confirmCleanupOpen: false,
  cleanup: { tenant: 'all', olderThanDays: 30, busy: false },
  clearDbBusy: false,

  get tenants() {
    return this.$store.tenants.list;
  },

  init() {
    this.loadDbInfo();
  },

  onPageShown(page) {
    if (page === 'settings') this.loadDbInfo();
  },

  // ── Tenants ──
  openTenantModal(tenant = null) {
    this.tenantModal.editing = !!tenant;
    this.tenantModal.form = tenant ? { ...tenant, client_secret: '' } : emptyTenant();
    this.tenantModal.serviceKey = '';
    this.tenantModal.serviceKeyError = '';
    this.tenantModal.open = true;
  },

  // Fill the tenant form from a CPI service key (BTP cockpit → service
  // instance → service key), which users usually have as JSON.
  applyServiceKey() {
    const m = this.tenantModal;
    if (!m.serviceKey.trim()) {
      m.serviceKeyError = '';
      return;
    }
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

  async saveTenant() {
    try {
      await api.tenants.save(this.tenantModal.form, !this.tenantModal.editing);
      this.tenantModal.open = false;
      await this.$store.tenants.load();
      this.$store.toast.notify('Tenant saved', 'success');
    } catch (e) {
      this.$store.toast.notify(`Couldn't save tenant: ${e.message}`, 'error');
    }
  },

  async testTenant(t) {
    try {
      await api.tenants.test(t.id);
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
      data = await api.tenants.remove(id, purge);
    } catch (e) {
      this.$store.toast.notify(`Couldn't delete the tenant: ${e.message}`, 'error');
      return;
    } finally {
      await this.$store.tenants.load();
      emit(SCHEDULES_CHANGED);
    }
    if (purge) {
      await this.loadDbInfo();
      emit(LOGS_CHANGED, { firstPage: true });
      this.$store.toast.notify(`Tenant deleted with ${data.deleted_entries.toLocaleString('en-US')} entries`);
    } else {
      this.$store.toast.notify('Tenant deleted, its log entries are kept');
    }
  },

  // ── Database ──
  async loadDbInfo() {
    try {
      this.dbInfo = await api.db.info();
    } catch (e) {
      console.error(e);
    }
  },

  async clearDb() {
    this.clearDbBusy = true;
    try {
      await api.db.clear();
      this.confirmClearOpen = false;
      await this.loadDbInfo();
      emit(LOGS_CHANGED, { firstPage: true });
      this.$store.toast.notify('Database cleared');
    } catch (e) {
      this.$store.toast.notify(`Couldn't clear the database: ${e.message}`, 'error');
    } finally {
      this.clearDbBusy = false;
    }
  },

  async cleanupLogs() {
    this.cleanup.busy = true;
    try {
      const data = await api.db.cleanup({ older_than_days: this.cleanup.olderThanDays, tenant: this.cleanup.tenant });
      this.confirmCleanupOpen = false;
      await this.loadDbInfo();
      emit(LOGS_CHANGED, { firstPage: true });
      this.$store.toast.notify(
        `Deleted ${data.deleted.toLocaleString('en-US')} entries (${data.remaining.toLocaleString('en-US')} remaining)`
      );
    } catch (e) {
      this.confirmCleanupOpen = false;
      this.$store.toast.notify(`Couldn't delete old entries: ${e.message}`, 'error');
    } finally {
      this.cleanup.busy = false;
    }
  },
});
