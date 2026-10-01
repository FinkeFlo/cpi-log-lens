const STORAGE_KEY = 'cpi-log-lens-theme';
export const THEME_CHANGED = 'theme-changed';

const validPreference = value => ['system', 'light', 'dark'].includes(value);

export default {
  preference: 'system',

  init() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      this.preference = validPreference(stored) ? stored : 'system';
    } catch (error) {
      console.warn('Theme preference is unavailable:', error);
    }
    this.media = window.matchMedia('(prefers-color-scheme: dark)');
    this.media.addEventListener('change', () => {
      if (this.preference === 'system') this.apply();
    });
    this.apply();
  },

  setPreference(value) {
    if (!validPreference(value)) throw new TypeError(`Invalid theme preference: ${value}`);
    if (value === this.preference) return;
    try {
      if (value === 'system') localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, value);
    } catch (error) {
      console.warn('Theme preference could not be saved:', error);
    }
    this.preference = value;
    this.apply();
  },

  apply() {
    const resolved = this.preference === 'system'
      ? (this.media.matches ? 'dark' : 'light')
      : this.preference;
    const theme = `lens-${resolved}`;
    if (document.documentElement.dataset.theme !== theme) {
      document.documentElement.dataset.theme = theme;
      window.dispatchEvent(new CustomEvent(THEME_CHANGED, { detail: theme }));
    }
  },
};
