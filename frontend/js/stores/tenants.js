import { api } from '../api.js';
import { describeError } from '../states.js';

// The configured tenants, shared by all pages (selects, checkboxes, settings).
export default {
  list: [],
  loaded: false,
  loading: false,
  error: null, // describeError() of the last failed load; pages show it with "Try again"

  init() {
    this.load();
  },

  async load() {
    this.loading = true;
    try {
      this.list = await api.tenants.list();
      this.loaded = true;
      this.error = null;
    } catch (e) {
      this.error = describeError(e, 'tenants');
    } finally {
      this.loading = false;
    }
  },

  name(id) {
    return this.list.find(t => t.id === id)?.name || id;
  },
};
