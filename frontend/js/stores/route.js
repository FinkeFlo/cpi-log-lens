// Hash routing: #browse, #fetch, #stats, #settings (Browse adds its filters, e.g. #browse?level=ERROR).
import { PAGE_SHOWN, emit } from '../events.js';

export const PAGES = ['browse', 'fetch', 'stats', 'settings'];

export function pageFromHash() {
  const page = window.location.hash.slice(1).split('?')[0];
  return PAGES.includes(page) ? page : 'browse';
}

export default {
  page: 'browse',

  init() {
    this.page = pageFromHash();
    // Keep the page in sync when the user presses back/forward
    window.addEventListener('hashchange', () => {
      const page = pageFromHash();
      if (page !== this.page) this.show(page);
    });
  },

  show(page) {
    this.page = page;
    emit(PAGE_SHOWN, page);
  },

  navigate(page) {
    this.show(page);
    // replaceState keeps the URL in sync without firing hashchange; Browse adds
    // its filters when it searches.
    if (page !== 'browse') history.replaceState(null, '', '#' + page);
  },
};
