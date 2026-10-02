# Changelog

## [0.3.0](https://github.com/FinkeFlo/cpi-log-lens/compare/v0.2.0...v0.3.0) (2026-10-02)


### Features

* **ui:** add product design system ([#37](https://github.com/FinkeFlo/cpi-log-lens/issues/37)) ([d5209f7](https://github.com/FinkeFlo/cpi-log-lens/commit/d5209f7b338ad0bd72e2d14676af5d5f5e62127b))
* **ui:** add shareable browse filters ([#38](https://github.com/FinkeFlo/cpi-log-lens/issues/38)) ([0502f2c](https://github.com/FinkeFlo/cpi-log-lens/commit/0502f2c8209d392d79dbafe8f1c119b913fa42c8))
* **ui:** add states, log detail drawer and per-tenant fetch progress ([#40](https://github.com/FinkeFlo/cpi-log-lens/issues/40)) ([8c6beb8](https://github.com/FinkeFlo/cpi-log-lens/commit/8c6beb83a3aae63b5c0720d4d706bb860915cfa0))
* **ui:** easier schedules and connection test in the tenant dialog ([#41](https://github.com/FinkeFlo/cpi-log-lens/issues/41)) ([e495a62](https://github.com/FinkeFlo/cpi-log-lens/commit/e495a622d3522a6f6d3b75d697639dfe75774976))
* **ui:** improve accessibility and mobile layout ([#39](https://github.com/FinkeFlo/cpi-log-lens/issues/39)) ([a9f0127](https://github.com/FinkeFlo/cpi-log-lens/commit/a9f0127723646b5a508ba7d49d999d83e110ac1b))
* **ui:** page the log list with newer and older and jump to a time ([#42](https://github.com/FinkeFlo/cpi-log-lens/issues/42)) ([24b0a57](https://github.com/FinkeFlo/cpi-log-lens/commit/24b0a572c5d49fe797acf867c08827513b0ffc9e))


### Bug Fixes

* **ui:** correct stats counts ([#31](https://github.com/FinkeFlo/cpi-log-lens/issues/31)) ([eb01a5e](https://github.com/FinkeFlo/cpi-log-lens/commit/eb01a5e815d1eae432ee2c80a67109746e48d2fd))


### Build and Dependencies

* **ui:** compile Tailwind and daisyUI CSS ([#35](https://github.com/FinkeFlo/cpi-log-lens/issues/35)) ([44320d3](https://github.com/FinkeFlo/cpi-log-lens/commit/44320d34e4b587b09954418c5f6581a9c3d16777))

## [0.2.0](https://github.com/FinkeFlo/cpi-log-lens/compare/v0.1.1...v0.2.0) (2026-10-01)


### Upgrade notes

* **Back up the database before upgrading.** On its first start, 0.2.0 migrates the database schema (to version 5, shown in `GET /api/db/info`). Running 0.1.x on a migrated database is not supported; to go back, restore the backup.
* **Storage format (optional, one-way).** New databases store the log texts ZSTD-compressed, about 2.5–4 times smaller. An existing database keeps its format until you convert it with `python -m app.storage` or by starting once with `DB_STORAGE_UPGRADE=true` (README, *Storage format*). The converted file can only be opened with DuckDB 1.5 or newer; the old file is kept as `cpi_logs.duckdb.bak-<UTC time>`.
* **API errors use HTTP status codes** with `{"detail": …}`: starting a fetch while one runs answers 409 (was 200 with `"ok": false`), an unknown tenant or schedule 404, a failed token request in the connection test 502, an unexpected error 500. Scripts that checked `"ok": false` need to check the status code.
* Deleting a tenant keeps its log entries and downloaded files; `DELETE /api/tenants/{id}?purge=true` (or the second question in the UI) deletes them too.
* `SCHEDULE_CHECK_SECONDS` is no longer read: schedules run at their due time.


### Features

* **db:** compressed log storage and database backups ([#9](https://github.com/FinkeFlo/cpi-log-lens/issues/9)) ([1fac83e](https://github.com/FinkeFlo/cpi-log-lens/commit/1fac83e9916693a6a8e6e3671d00e31d31307483))
* **db:** versioned schema migrations ([#8](https://github.com/FinkeFlo/cpi-log-lens/issues/8)) ([110f517](https://github.com/FinkeFlo/cpi-log-lens/commit/110f517df6252206275e770e53a17e1e2cfe09f8))
* **fetch:** persistent fetch run history and a locked job start ([#12](https://github.com/FinkeFlo/cpi-log-lens/issues/12)) ([30e6255](https://github.com/FinkeFlo/cpi-log-lens/commit/30e625554ee0894a93d7eb2deb61be7472b1ced7))


### Bug Fixes

* **api:** http status codes and one error schema ([#14](https://github.com/FinkeFlo/cpi-log-lens/issues/14)) ([d0eb86f](https://github.com/FinkeFlo/cpi-log-lens/commit/d0eb86f47d2adc62fb069a4bf5376eba06460037))
* import bookkeeping per log type and consistent tenant deletion ([#11](https://github.com/FinkeFlo/cpi-log-lens/issues/11)) ([f0cb0d5](https://github.com/FinkeFlo/cpi-log-lens/commit/f0cb0d570c4f26849131360668be7514742b2189))
* **scheduler:** run schedules and retention with apscheduler ([#13](https://github.com/FinkeFlo/cpi-log-lens/issues/13)) ([b36eca9](https://github.com/FinkeFlo/cpi-log-lens/commit/b36eca93b0484172b69a62501739463a06c83cea))


### Build and Dependencies

* python tooling with uv, ruff and mypy ([#4](https://github.com/FinkeFlo/cpi-log-lens/issues/4)) ([27ea0c9](https://github.com/FinkeFlo/cpi-log-lens/commit/27ea0c9d95f4a225355f087b67bfd811fc4f84ae))


### Documentation

* point changelog entries to the current commit history ([#1](https://github.com/FinkeFlo/cpi-log-lens/issues/1)) ([4d39161](https://github.com/FinkeFlo/cpi-log-lens/commit/4d391613446e15f2134ed9d64429f2160464fc45))

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
