# CPI Log Lens

A self-hosted web application for downloading, storing, and analyzing SAP Cloud Integration (CPI) logs.

## Features

- 📥 **Fetch** — download HTTP and trace logs from any number of CPI tenants via OAuth2
- 🔍 **Browse** — search and filter log entries by level, IFlow, message content and date range
- 📊 **Stats** — error distribution per IFlow, level breakdown, hourly timeline
- ⚙️ **Settings** — manage tenant credentials via UI or `tenants.jsonc`
- 🐳 **Docker** — runs anywhere with `docker compose up`
- 🧪 **Mock mode** — works without a real CPI connection for local development

## Quick Start

```bash
# 1. Clone
git clone https://github.com/FinkeFlo/cpi-log-lens
cd cpi-log-lens

# 2. Configure tenants
cp tenants.jsonc.example tenants.jsonc
# Edit tenants.jsonc — add your CPI tenant credentials

# 3. Start
docker compose up

# 4. Open
open http://localhost:8080
```

## Tenant Configuration

Credentials are stored in **`tenants.jsonc`** (JSON with comments, gitignored by default).

```jsonc
{
  "tenants": [
    {
      // Short ID used in the UI (lowercase, no spaces)
      "id": "dev",
      "name": "DEV",

      // From SAP BTP → Instances & Subscriptions → Cloud Integration → Service Key
      "api_url":      "https://your-tenant.it-cpi018.cfapps.eu10-003.hana.ondemand.com",
      "oauth_url":    "https://your-tenant.authentication.eu10.hana.ondemand.com/oauth/token",
      "client_id":    "sb-your-client-id",
      "client_secret": "your-client-secret"
    }
    // Add as many tenants as you need
  ]
}
```

You can also add and manage tenants directly in the **Settings** page of the web UI.

## Mock Mode

To run without a real CPI connection (for demos or local development):

```bash
MOCK=true docker compose up
```

Imports the sample log files from `backend/mock/` instead of calling the CPI API.

## Log Format

CPI uses a SAP-proprietary `#`-delimited format with 15 fields:

```
Timestamp # Timezone # Level # Logger # User # IFlow # Category # ... # Message # - # IP # Node
```

All fields are parsed and indexed into a local SQLite database for fast querying.

## Data Storage

| Path | Contents |
|---|---|
| `data/cpi_logs.db` | SQLite database (all imported log entries) |
| `data/logs/<tenant>/` | Raw downloaded log files (gzip) |
| `tenants.jsonc` | Tenant credentials — **do not commit** |

Both `data/` and `tenants.jsonc` are excluded from git.

## Stack

- **Backend** — Python, FastAPI, aiosqlite, httpx
- **Frontend** — Alpine.js, Tailwind CSS, DaisyUI, Chart.js
- **Storage** — SQLite
- **Runtime** — Docker / Docker Compose

## License

MIT
