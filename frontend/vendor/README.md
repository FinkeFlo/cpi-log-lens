# Vendored frontend libraries

Pinned JavaScript libraries served by the app itself, so the UI works without
internet access. Do not edit by hand — regenerate with `scripts/vendor-frontend.sh`
(downloads are verified by SHA-256). The stylesheet is generated separately by
`scripts/build-css.sh` during the image build and is not committed.

| File | Library | Version | License |
|---|---|---|---|
| `alpine.esm.min.js` | [Alpine.js](https://alpinejs.dev) (ES module build) | 3.17.4 | MIT |
| `chart.umd.min.js` | [Chart.js](https://www.chartjs.org) | 4.4.0 | MIT |

CSS is built with Tailwind CSS 4.3.3 (standalone CLI) and daisyUI 5.7.47 (standalone plugin).
Their exact release artifacts and SHA-256 checksums are pinned in `scripts/build-css.sh`.
