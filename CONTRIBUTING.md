# Contributing

Thanks for your interest in improving CPI Log Lens.

## Development setup

Requirements: Docker. To run the backend directly, [uv](https://docs.astral.sh/uv/) (it installs a
matching Python if needed).

```bash
# Live reload of backend and frontend from the working tree
docker compose -f docker-compose.yml -f compose.dev.yaml up --build
```

Try your changes without a CPI tenant: use **Try demo data** on the start page, or run with
`MOCK=true`.

Without Docker:

```bash
cd backend
uv sync        # .venv with the locked dependencies and the dev tools
DB_PATH=../data/cpi_logs.duckdb LOGS_DIR=../data/logs TENANTS_CONFIG=../config/tenants.jsonc \
  uv run uvicorn app.main:app --reload --port 8080
```

Python dependencies are declared in `backend/pyproject.toml` and pinned, including all transitive
packages, in `backend/uv.lock`. After changing them, run `uv lock` and commit both files; CI and the
image build fail if the lock file is out of date.

Before pushing, run the checks CI runs (in `backend/`):

```bash
uv run ruff check .      # lint (add --fix for automatic fixes)
uv run ruff format .     # formatting
uv run mypy .            # type check
uv run pytest            # tests
```

**Schema changes** go into a new migration file in `backend/app/migrations/` with the next number:
`vNNNN_<what>.sql`, or `vNNNN_<what>.py` with an `upgrade(conn)` function for data changes. Each
migration runs once, in its own transaction, recorded in `schema_version`. Never edit a migration
that has been released; add a new one.

Tests live in `backend/tests/`. Each test gets a fresh DuckDB file and a started app, called through
httpx without a server; CPI requests go to an in-process fake CPI server (`app/cpi/fake.py`), so no
tenant is needed. Use synthetic log lines only (`log_line()` / `numbered_lines()`). A known bug can be pinned
with `@pytest.mark.xfail(reason="<finding or issue>: …")`; xfail is strict, so remove the marker in the
change that fixes it.

The frontend has no build step. Its libraries are vendored in `frontend/vendor/`. After using new
Tailwind classes, or to bump a library version, run `scripts/vendor-frontend.sh` (downloads are
checksum-verified; no Node.js needed).

## Pull requests

- One logical change per pull request, branched from an up-to-date `main`.
- Branch names: `<type>/<short-description>`, e.g. `fix/stats-tenant-count`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org):
  `<type>(<scope>): <imperative summary>`. Types: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`,
  `build`, `ci`, `chore`, `style`. Scopes: `api`, `db`, `fetch`, `parser`, `scheduler`, `ui`,
  `docker`, `ci`, `docs`. The body explains *why*. Release notes and versions are generated from
  these messages.
- Keep refactoring and behavior changes in separate commits.
- CI must pass (lint, formatting, type check, tests, dependency audit, image build and smoke test). Describe how you
  tested the change in the pull request.
- Update the README configuration table when you add or change an environment variable.

## Reporting bugs

Use the issue templates. **Never include credentials, service keys or real log lines**; they can
contain customer data. Anonymize excerpts or reproduce with the demo data.

Security issues: see [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
