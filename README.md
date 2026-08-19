# CPI Log Explorer

A self-hosted web application for downloading, storing, and analyzing SAP Cloud Integration (CPI) logs.

## Features

- 📥 **Fetch** — download HTTP and trace logs from CPI tenants via OAuth2
- 🔍 **Browse** — search and filter log entries by level, IFlow, message content and date range
- 📊 **Stats** — error distribution per IFlow, level breakdown, hourly timeline
- ⚙ **Settings** — manage tenant credentials via UI or `.env` file
- 🐳 **Docker** — runs anywhere with `docker compose up`
- 🧪 **Mock mode** — works without a real CPI connection for local development

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-org/cpi-log-explorer
cd cpi-log-explorer

# 2. Configure credentials
cp .env.example .env
# Edit .env and fill in your CPI tenant credentials

# 3. Start
docker compose up

# 4. Open http://localhost:8080
```

## Configuration

All configuration is done via the `.env` file. Copy `.env.example` to `.env` and fill in your values.

You can also add and edit tenants directly in the **Settings** page of the web UI.
Credentials entered via the UI are stored encrypted in the local SQLite database.

### Tenant variables

For each tenant, define the following (replace `DEV` with your tenant prefix):

```
CPI_DEV_NAME=DEV
CPI_DEV_API_URL=https://<your-tenant>.cfapps.<region>.hana.ondemand.com
CPI_DEV_OAUTH_URL=https://<your-tenant>.authentication.<region>.hana.ondemand.com/oauth/token
CPI_DEV_CLIENT_ID=sb-<your-client-id>
CPI_DEV_CLIENT_SECRET=<your-client-secret>
```

You can define as many tenants as you need. The prefix (e.g. `DEV`, `QAS`, `PRD`) becomes the tenant ID.

### Mock mode

To run without a real CPI connection (e.g. for local development or demos):

```
MOCK=true
```

Mock mode imports the sample log files from `backend/mock/` instead of calling the CPI API.

## Log Format

CPI logs use a SAP-proprietary `#`-delimited format:

```
YYYY-MM-DD HH:MM:SS # timezone # level # logger # user # iflow # category # ... # message # - # ip # node
```

The application parses and indexes all fields into a local SQLite database for fast querying.

## Development

```bash
# Without Docker (requires Python 3.12+)
cd backend
pip install -r requirements.txt
cp ../.env.example .env   # fill in values
uvicorn main:app --reload --port 8080
```

## Data Storage

- `data/cpi_logs.db` — SQLite database (all imported log entries)
- `data/logs/<tenant>/` — raw downloaded log files (gzip)

Both are excluded from git and persisted as Docker volumes.

## License

MIT
