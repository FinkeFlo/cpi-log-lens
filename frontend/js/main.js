// Entry point: registers the shared stores and the page components, then starts Alpine.
// Native ES modules, no build step; libraries are vendored in /vendor.
import Alpine from '../vendor/alpine.esm.min.js';

import browsePage from './pages/browse.js';
import fetchPage from './pages/fetch.js';
import settingsPage from './pages/settings.js';
import statsPage from './pages/stats.js';
import fetchJob from './stores/fetchJob.js';
import route from './stores/route.js';
import tenants from './stores/tenants.js';
import theme from './stores/theme.js';
import toast from './stores/toast.js';

Alpine.store('toast', toast);
Alpine.store('route', route);
Alpine.store('tenants', tenants);
Alpine.store('fetchJob', fetchJob);
Alpine.store('theme', theme);

Alpine.data('browsePage', browsePage);
Alpine.data('fetchPage', fetchPage);
Alpine.data('statsPage', statsPage);
Alpine.data('settingsPage', settingsPage);

window.Alpine = Alpine;
Alpine.start();
// Pick up a fetch job that is still running (after a reload or from another tab).
Alpine.store('fetchJob').reconnect();
