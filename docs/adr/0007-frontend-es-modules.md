# 7. Frontend as native ES modules with Alpine stores

Status: accepted

## Context

The whole UI was one Alpine component, `App()` in `frontend/app.js` (about 750 lines): every page,
dialog, the fetch job state and the API calls shared one scope. A change to one page could break
another through a shared field name, and there was no single place for API errors.

The app runs on the user's machine and in a small container; it has no Node.js toolchain, and the
frontend libraries are vendored so the UI works offline.

## Decision

Split the frontend into native ES modules, loaded by the browser without a build step:

- `js/api.js` is the only module that calls the API; it throws `ApiError` with the server's
  message for every non-2xx answer.
- Shared state lives in Alpine stores (`js/stores/`): the current page (hash routing), the
  tenants, the fetch job (fed by the server-sent events) and the toast.
- Each page is an `Alpine.data` component (`js/pages/`) with its own state and dialogs. Pages do
  not call each other; they send window events (`js/events.js`), e.g. "log entries changed".
- `js/main.js` imports the vendored ES module build of Alpine, registers stores and pages and
  starts Alpine. Chart.js stays a classic script.

A bundler (Vite, esbuild) would add a Node.js build to the image and to contributing, for a UI of a
few hundred lines per page; browsers load a dozen small modules from the same origin fast enough.

## Consequences

- No build step: edit a file, reload the page. CI checks that every module parses.
- Every `.js` file is served with `Cache-Control: no-cache`, so a new version is picked up on
  reload without cache-busting file names.
- `scripts/vendor-frontend.sh` vendors `alpine.esm.min.js` and scans `frontend/js/` for Tailwind
  classes.
