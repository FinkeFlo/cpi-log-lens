# Vendored frontend libraries

Pinned copies served by the app itself, so the UI works without internet
access. Do not edit by hand — regenerate with `scripts/vendor-frontend.sh`
(downloads are verified by SHA-256; `tailwind.min.css` is built from the
classes used in `frontend/index.html` and `frontend/js/`).

| File | Library | Version | License |
|---|---|---|---|
| `alpine.esm.min.js` | [Alpine.js](https://alpinejs.dev) (ES module build) | 3.17.4 | MIT |
| `chart.umd.min.js` | [Chart.js](https://www.chartjs.org) | 4.4.0 | MIT |
| `daisyui.full.min.css` | [daisyUI](https://daisyui.com) | 4.12.10 | MIT |
| `tailwind.min.css` | [Tailwind CSS](https://tailwindcss.com) (standalone CLI build) | 3.4.17 | MIT |
