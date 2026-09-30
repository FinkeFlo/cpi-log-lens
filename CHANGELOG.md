# Changelog

## [0.1.1](https://github.com/FinkeFlo/cpi-log-lens/compare/v0.1.0...v0.1.1) (2026-09-30)


### Bug Fixes

* **docker:** apply debian security updates and scan images in ci ([#24](https://github.com/FinkeFlo/cpi-log-lens/issues/24)) ([f0568d1](https://github.com/FinkeFlo/cpi-log-lens/commit/f0568d13b6c8ec8e23baf0cc38ed1c36f94d1fe4))

## 0.1.0 (2026-09-30)


### Features

* concurrent CPI downloads with retry, shared client, and job cancel ([19fa6cb](https://github.com/FinkeFlo/cpi-log-lens/commit/19fa6cbe83b2b9bbf51dd250ea93689afaf6568a))
* english ui and product name cpi log lens ([#17](https://github.com/FinkeFlo/cpi-log-lens/issues/17)) ([b704767](https://github.com/FinkeFlo/cpi-log-lens/commit/b704767ba840076b69ff86577544f60b5fca8f0b))
* first-run setup, demo data and service key paste ([#22](https://github.com/FinkeFlo/cpi-log-lens/issues/22)) ([977ae4b](https://github.com/FinkeFlo/cpi-log-lens/commit/977ae4b9948ae97cf9d183f97c3172f95a576717))
* health endpoints, structured logging and an event-loop watchdog ([#10](https://github.com/FinkeFlo/cpi-log-lens/issues/10)) ([32b6b99](https://github.com/FinkeFlo/cpi-log-lens/commit/32b6b99ac048d41c5706ecedfafef540443b89c4))
* initial release — CPI Log Lens ([636c7d7](https://github.com/FinkeFlo/cpi-log-lens/commit/636c7d774a4af5c338ad7386558d6406b6b47a3f))
* lossless log import — raw_line, multi-line merge, unparsed fallback ([d214890](https://github.com/FinkeFlo/cpi-log-lens/commit/d2148907485000523a44b8843f778da4dc1d630d))
* migrate backend to DuckDB, fix dev-mode reliability issues ([a827c12](https://github.com/FinkeFlo/cpi-log-lens/commit/a827c12b9aa51ddd472a2cb8da9b53c8dc0e159e))
* optional automatic log retention ([00f0154](https://github.com/FinkeFlo/cpi-log-lens/commit/00f0154dd27de0f2a89c14e4d14018e158165d4d))
* recurring fetch schedules + saved default fetch config ([2b0735e](https://github.com/FinkeFlo/cpi-log-lens/commit/2b0735ef529df4e056dc8b5daffa47bfdd42f86e))
* time out and bound database reads ([#9](https://github.com/FinkeFlo/cpi-log-lens/issues/9)) ([8a18dbf](https://github.com/FinkeFlo/cpi-log-lens/commit/8a18dbfb89ce8a90ac23a4b5c29a3400baab3be8))


### Bug Fixes

* /api/query date_to 500 error with full datetime values ([471f343](https://github.com/FinkeFlo/cpi-log-lens/commit/471f343ab7d655eeb26d46912a94db4c3d9c8a94))
* Browse/Fetch page layout overflow ([dee3f26](https://github.com/FinkeFlo/cpi-log-lens/commit/dee3f26d349d7824a47dbf7c09ec817078ae6fa8))
* keep client secret when editing a tenant ([#3](https://github.com/FinkeFlo/cpi-log-lens/issues/3)) ([49c3d05](https://github.com/FinkeFlo/cpi-log-lens/commit/49c3d051ac58373e68e0e571e59245f4311692be))
* keep duckdb and the container inside a memory budget ([#5](https://github.com/FinkeFlo/cpi-log-lens/issues/5)) ([6ee28c8](https://github.com/FinkeFlo/cpi-log-lens/commit/6ee28c86cf857348fe87d9be26f01414ea9b33e2))
* run the container without uvicorn --reload ([#4](https://github.com/FinkeFlo/cpi-log-lens/issues/4)) ([845f8e6](https://github.com/FinkeFlo/cpi-log-lens/commit/845f8e691c3535284808a9ba9cda9b0557bb331d))
* safe file paths and validated request input ([#13](https://github.com/FinkeFlo/cpi-log-lens/issues/13)) ([19bbb17](https://github.com/FinkeFlo/cpi-log-lens/commit/19bbb178ab4028c2d25b9cdff070bd17fdec959b))
* safe network defaults ([#12](https://github.com/FinkeFlo/cpi-log-lens/issues/12)) ([d9ff431](https://github.com/FinkeFlo/cpi-log-lens/commit/d9ff43143928a78fc38ff680dcbcbe577c360be8))
* separate read/write DB paths so UI stays responsive during imports ([edf4549](https://github.com/FinkeFlo/cpi-log-lens/commit/edf4549891a92e52228757c4da619fa0773678e7))
* stream downloads and import log files in batches off the event loop ([#8](https://github.com/FinkeFlo/cpi-log-lens/issues/8)) ([e0d673a](https://github.com/FinkeFlo/cpi-log-lens/commit/e0d673a29ca8c191f5565748f52bd1f578208dc5))
* unblock duckdb checkpoints and bound write-lock waits ([#6](https://github.com/FinkeFlo/cpi-log-lens/issues/6)) ([314204d](https://github.com/FinkeFlo/cpi-log-lens/commit/314204d714fcb19ba831eb4825a06eb3de393d43))


### Performance

* drop unused indexes, batch-insert with RETURNING for faster imports ([bfee583](https://github.com/FinkeFlo/cpi-log-lens/commit/bfee583e6cd524be67f8ad551eb4d8280df10727))
* lighter log list and cached stats ([#11](https://github.com/FinkeFlo/cpi-log-lens/issues/11)) ([69e9463](https://github.com/FinkeFlo/cpi-log-lens/commit/69e9463b19f8623d19349ffa98f9a590e364944a))
* native TIMESTAMP column, drop UNIQUE constraint, pyarrow bulk insert ([1f1f90c](https://github.com/FinkeFlo/cpi-log-lens/commit/1f1f90c80940e698f218863aa6ce13e984ab94b6))


### Build and Dependencies

* docker compose up without required files ([#18](https://github.com/FinkeFlo/cpi-log-lens/issues/18)) ([ae28125](https://github.com/FinkeFlo/cpi-log-lens/commit/ae281251f4c17cb704bb66be15f2e9aabb1ce3e6))
* **docker:** production image with frontend, non-root ([#16](https://github.com/FinkeFlo/cpi-log-lens/issues/16)) ([afd270e](https://github.com/FinkeFlo/cpi-log-lens/commit/afd270e03ca1b0b90f1431c2522e556f62fd00d0))
* **ui:** serve pinned frontend libraries locally ([#15](https://github.com/FinkeFlo/cpi-log-lens/issues/15)) ([9011cb2](https://github.com/FinkeFlo/cpi-log-lens/commit/9011cb2ed1950ebb8ad2f7ddce603e202e0f0a51))
* upgrade dependencies and drop unused ones ([#14](https://github.com/FinkeFlo/cpi-log-lens/issues/14)) ([7928977](https://github.com/FinkeFlo/cpi-log-lens/commit/7928977365aa7ab298099a07934e5d3b5af3dcc3))
* upgrade duckdb to 1.5 and raise the checkpoint threshold ([#7](https://github.com/FinkeFlo/cpi-log-lens/issues/7)) ([6e4d291](https://github.com/FinkeFlo/cpi-log-lens/commit/6e4d291757254e063fbfb671a5d471a38baa7bd9))


### Documentation

* add mit license file ([#2](https://github.com/FinkeFlo/cpi-log-lens/issues/2)) ([b757f27](https://github.com/FinkeFlo/cpi-log-lens/commit/b757f27f55f82b12c783fc4a9426c6974895fe1a))
* readme, community files, templates and ADRs ([#23](https://github.com/FinkeFlo/cpi-log-lens/issues/23)) ([7034f13](https://github.com/FinkeFlo/cpi-log-lens/commit/7034f13654347a590166ce22f46ad8dc24b6a137))
* update README — tenants.jsonc, quick start, stack ([dc3b33c](https://github.com/FinkeFlo/cpi-log-lens/commit/dc3b33cf0a6a22e208dcecf7ef4b582979ba037e))
