// Hash routing: #browse, #fetch, #stats, #settings (Browse adds its filters, e.g. #browse?level=ERROR).
import { PAGE_SHOWN, emit } from '../events.js';

export const PAGES = ['browse', 'fetch', 'stats', 'settings'];
const PAGE_TITLES = {
  browse: 'Browse logs',
  fetch: 'Fetch logs',
  stats: 'Statistics',
  settings: 'Settings',
};

function setPageTitle(page) {
  document.title = `${PAGE_TITLES[page]} | CPI Log Lens`;
}

export function pageFromHash() {
  const page = window.location.hash.slice(1).split('?')[0];
  return PAGES.includes(page) ? page : 'browse';
}

export default {
  page: 'browse',
  lastHash: '',
  mobileNavOpen: false,

  init() {
    this.page = pageFromHash();
    this.lastHash = window.location.hash;
    setPageTitle(this.page);
    const sync = () => {
      const hash = window.location.hash;
      if (hash === this.lastHash) return;
      this.lastHash = hash;
      this.page = pageFromHash();
      this.mobileNavOpen = false;
      setPageTitle(this.page);
      emit(PAGE_SHOWN, this.page);
    };
    window.addEventListener('popstate', sync);
    window.addEventListener('hashchange', sync);
  },

  show(page) {
    this.page = page;
    this.mobileNavOpen = false;
    setPageTitle(page);
    emit(PAGE_SHOWN, page);
  },

  writeHash(hash, replace = false) {
    if (window.location.hash === hash) {
      this.lastHash = hash;
      return false;
    }
    history[replace ? 'replaceState' : 'pushState'](null, '', hash);
    this.lastHash = hash;
    return true;
  },

  back() {
    history.back();
  },

  navigate(page) {
    this.writeHash('#' + page);
    this.show(page);
  },
};
