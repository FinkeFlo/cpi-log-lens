// Events between the pages (window CustomEvents, so Alpine templates can listen with
// @name.window): the pages stay independent of each other.

// Entries were imported or deleted. detail: { firstPage: bool, visibleOnly: bool }; Browse reloads
// its list (page 1 or the current page), with visibleOnly only while it is shown.
export const LOGS_CHANGED = 'logs-changed';
export const BROWSE_IFLOW = 'browse-iflow'; // detail: {iflow, level} — show matching entries in Browse
export const OPEN_TENANT_MODAL = 'open-tenant-modal'; // open the "Add tenant" dialog in Settings
export const PAGE_SHOWN = 'page-shown'; // detail: page name — a page was navigated to
export const SCHEDULES_CHANGED = 'schedules-changed'; // schedules were changed outside the Fetch page

export function emit(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}
