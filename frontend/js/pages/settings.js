// Settings page: tenants (dialog, service key paste, connection test, delete) and the database.
import { api } from '../api.js';
import { connectionTestError, connectionTestResult, missingForTest } from '../connection.js';
import { LOGS_CHANGED, SCHEDULES_CHANGED, emit } from '../events.js';
import { describeError } from '../states.js';

const emptyTenant = () => ({ id: '', name: '', api_url: '', oauth_url: '', client_id: '', client_secret: '' });
const idleTest = () => ({ busy: false, result: null }); // result: { ok, title, hint }

export default () => ({
  tenantModal: {
    open: false,
    editing: false,
    form: emptyTenant(),
    serviceKey: '',
    serviceKeyError: '',
    test: idleTest(),
  },
  _testRun: 0, // counts connection tests in the dialog; an answer to an older one is dropped
  tenantDeleteModal: { open: false, tenantId: '', purgeLogs: false },
  dbInfo: {},
  dbInfoError: null, // describeError() of the last failed load
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
    this.resetConnectionTest();
    this.tenantModal.open = true;
  },

  confirmDeleteTenant(id) {
    this.tenantDeleteModal = { open: true, tenantId: id, purgeLogs: false };
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
      this.resetConnectionTest(); // a result (or a test still running) is about the old values
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

  // Test the details in the dialog without saving them. Editing them drops the result.
  async testConnection() {
    const m = this.tenantModal;
    const missing = missingForTest(m.form, m.editing);
    if (missing.length) {
      m.test = { busy: false, result: { ok: false, title: 'Fill in the connection details first.', hint: `Missing: ${missing.join(', ')}.` } };
      return;
    }
    const run = ++this._testRun;
    m.test = { busy: true, result: null };
    const f = m.form;
    const details = { api_url: f.api_url, oauth_url: f.oauth_url, client_id: f.client_id, client_secret: f.client_secret };
    if (m.editing) details.id = f.id; // an empty secret means the saved one
    let result;
    try {
      result = connectionTestResult(await api.tenants.testDetails(details));
    } catch (e) {
      result = connectionTestError(e);
    }
    if (run === this._testRun) m.test = { busy: false, result };
  },

  resetConnectionTest() {
    this._testRun++;
    this.tenantModal.test = idleTest();
  },

  async testTenant(t) {
    t._testing = true;
    t._test = null;
    try {
      t._test = connectionTestResult(await api.tenants.test(t.id));
    } catch (e) {
      t._test = connectionTestError(e);
    } finally {
      t._testing = false;
    }
  },

  async deleteTenant(id, purge) {
    this.tenantDeleteModal.open = false;
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
      this.dbInfoError = null;
    } catch (e) {
      this.dbInfoError = describeError(e, 'database details');
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
