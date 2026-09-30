# Changelog

## [0.1.1](https://github.com/FinkeFlo/cpi-log-lens/compare/v0.1.0...v0.1.1) (2026-09-30)


### Bug Fixes

* **docker:** apply debian security updates and scan images in ci ([e28ee28](https://github.com/FinkeFlo/cpi-log-lens/commit/e28ee2887b438138b408be406f7b44dc1cbf2847))

## 0.1.0 (2026-09-30)


### Features

* concurrent CPI downloads with retry, shared client, and job cancel ([19fa6cb](https://github.com/FinkeFlo/cpi-log-lens/commit/19fa6cbe83b2b9bbf51dd250ea93689afaf6568a))
* english ui and product name cpi log lens ([9cc0528](https://github.com/FinkeFlo/cpi-log-lens/commit/9cc0528d48a78bcd7e9b9d7b53749d6bb223116a))
* first-run setup, demo data and service key paste ([82b96b9](https://github.com/FinkeFlo/cpi-log-lens/commit/82b96b9d7d8f13239fca5df7320211b1cfc3161a))
* health endpoints, structured logging and an event-loop watchdog ([e90b13a](https://github.com/FinkeFlo/cpi-log-lens/commit/e90b13a24a4f4d60cda51cf4457de8cfe29f5474))
* initial release — CPI Log Lens ([636c7d7](https://github.com/FinkeFlo/cpi-log-lens/commit/636c7d774a4af5c338ad7386558d6406b6b47a3f))
* lossless log import — raw_line, multi-line merge, unparsed fallback ([d214890](https://github.com/FinkeFlo/cpi-log-lens/commit/d2148907485000523a44b8843f778da4dc1d630d))
* migrate backend to DuckDB, fix dev-mode reliability issues ([a827c12](https://github.com/FinkeFlo/cpi-log-lens/commit/a827c12b9aa51ddd472a2cb8da9b53c8dc0e159e))
* optional automatic log retention ([00f0154](https://github.com/FinkeFlo/cpi-log-lens/commit/00f0154dd27de0f2a89c14e4d14018e158165d4d))
* recurring fetch schedules + saved default fetch config ([2b0735e](https://github.com/FinkeFlo/cpi-log-lens/commit/2b0735ef529df4e056dc8b5daffa47bfdd42f86e))
* time out and bound database reads ([0493f81](https://github.com/FinkeFlo/cpi-log-lens/commit/0493f817364a085b1ad91112cd72ca8dfafef49b))


### Bug Fixes

* /api/query date_to 500 error with full datetime values ([471f343](https://github.com/FinkeFlo/cpi-log-lens/commit/471f343ab7d655eeb26d46912a94db4c3d9c8a94))
* Browse/Fetch page layout overflow ([dee3f26](https://github.com/FinkeFlo/cpi-log-lens/commit/dee3f26d349d7824a47dbf7c09ec817078ae6fa8))
* keep client secret when editing a tenant ([00ff67b](https://github.com/FinkeFlo/cpi-log-lens/commit/00ff67bc4a8a20b6c962a9a28cf0b2bafc7c2e3a))
* keep duckdb and the container inside a memory budget ([295320c](https://github.com/FinkeFlo/cpi-log-lens/commit/295320cea2707cd4ef035a9282ab05cf32e6524a))
* run the container without uvicorn --reload ([b2be75b](https://github.com/FinkeFlo/cpi-log-lens/commit/b2be75b9e7d14e07222cd71d43b874fb982f7848))
* safe file paths and validated request input ([8c041a0](https://github.com/FinkeFlo/cpi-log-lens/commit/8c041a0e46f264c9fcd5917b163a3c6a72773751))
* safe network defaults ([ff1cd65](https://github.com/FinkeFlo/cpi-log-lens/commit/ff1cd657fc5e092cef03f71f13112d5bc3fd96fb))
* separate read/write DB paths so UI stays responsive during imports ([edf4549](https://github.com/FinkeFlo/cpi-log-lens/commit/edf4549891a92e52228757c4da619fa0773678e7))
* stream downloads and import log files in batches off the event loop ([71b88fc](https://github.com/FinkeFlo/cpi-log-lens/commit/71b88fc00a981054475259e742547a37e27d2439))
* unblock duckdb checkpoints and bound write-lock waits ([7b5e37f](https://github.com/FinkeFlo/cpi-log-lens/commit/7b5e37f1c7b5570ed9fd3f84d15b11b548f86caf))


### Performance

* drop unused indexes, batch-insert with RETURNING for faster imports ([bfee583](https://github.com/FinkeFlo/cpi-log-lens/commit/bfee583e6cd524be67f8ad551eb4d8280df10727))
* lighter log list and cached stats ([090b97e](https://github.com/FinkeFlo/cpi-log-lens/commit/090b97eb6a3fd4e44fd46881fa2812fa0a8cd3a3))
* native TIMESTAMP column, drop UNIQUE constraint, pyarrow bulk insert ([1f1f90c](https://github.com/FinkeFlo/cpi-log-lens/commit/1f1f90c80940e698f218863aa6ce13e984ab94b6))


### Build and Dependencies

* docker compose up without required files ([beea3dd](https://github.com/FinkeFlo/cpi-log-lens/commit/beea3ddfe41de10ef4853b225540ef6c07d4be1b))
* **docker:** production image with frontend, non-root ([0b26af6](https://github.com/FinkeFlo/cpi-log-lens/commit/0b26af6f9c01c7762ec803c48f76823d955a798b))
* **ui:** serve pinned frontend libraries locally ([28805cb](https://github.com/FinkeFlo/cpi-log-lens/commit/28805cbf54f38fac51449dfbcc035441b1438f5d))
* upgrade dependencies and drop unused ones ([0af6f50](https://github.com/FinkeFlo/cpi-log-lens/commit/0af6f509b512df6ad733892bc00d320d204815ed))
* upgrade duckdb to 1.5 and raise the checkpoint threshold ([7623905](https://github.com/FinkeFlo/cpi-log-lens/commit/76239052661625cadfc52e254b71d99423201421))


### Documentation

* add mit license file ([0d5efaa](https://github.com/FinkeFlo/cpi-log-lens/commit/0d5efaa180b61baec336ecab9bddf8a0ca961044))
* readme, community files, templates and ADRs ([1001ba0](https://github.com/FinkeFlo/cpi-log-lens/commit/1001ba0fb0b1b77def28d021121fdcc84f018ea9))
* update README — tenants.jsonc, quick start, stack ([dc3b33c](https://github.com/FinkeFlo/cpi-log-lens/commit/dc3b33cf0a6a22e208dcecf7ef4b582979ba037e))
