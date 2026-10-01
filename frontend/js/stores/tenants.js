import { api } from '../api.js';

// The configured tenants, shared by all pages (selects, checkboxes, settings).
export default {
  list: [],
  loaded: false,

  init() {
    this.load();
  },

  async load() {
    try {
      this.list = await api.tenants.list();
      this.loaded = true;
    } catch (e) {
      console.error('loadTenants:', e);
    }
  },

  name(id) {
    return this.list.find(t => t.id === id)?.name || id;
  },
};
