# Deploying RMM Alerts with HTTPS (Let's Encrypt)

This project uses **Caddy** as the reverse proxy. Caddy obtains and renews Let's Encrypt certificates automatically—no certbot or manual steps.

## 1. Point your domain to the server

Create an **A record** (and optionally **AAAA** for IPv6) for your domain pointing to the public IP of the host running Docker:

- `rmm.yourdomain.com` → `YOUR_SERVER_IP`

Ensure **ports 80 and 443** are open on the host (firewall and any cloud security groups).

## 2. Configure environment

In `.env` (or export before `docker compose up`):

```bash
# Required for Let's Encrypt
DOMAIN=rmm.yourdomain.com
ACME_EMAIL=you@example.com   # used for Let's Encrypt expiry notices

# Datto: two site UIDs (comma-separated) — from GET /api/v2/account/sites in Swagger
DATTO_SITE_UIDS=site-uid-one,site-uid-two

# Optional: stay under Datto rate limit (default 550 requests/minute)
# DATTO_MAX_REQUESTS_PER_MINUTE=550

# List fetch: max HTTP pages per site per state (open and resolved each). Default 40 (~10k rows/state).
# Set to 0 for unlimited (full history — slow for very large tenants).
# DATTO_ALERT_LIST_MAX_PAGES=40

# Detail+ingest parallelism (each worker uses its own DB session). Default 1; try 3–5 if DB pool allows.
# DATTO_INGEST_CONCURRENCY=1

# Only ingest alerts on or after this instant (America/New_York; date-only = midnight ET). Default 2026-01-01.
# ALERT_INGEST_SINCE=2026-01-01
# ALERT_INGEST_SINCE=2026-01-15T08:00:00
```

Keep your existing `DATABASE_URL`, `DATTO_API_*` variables as needed.

`docker-compose.yml` passes **`DATTO_ALERT_LIST_MAX_PAGES`** and **`DATTO_INGEST_CONCURRENCY`** into the `web` service (with the same defaults as the app). Set them in `.env` to override; `docker compose up` will apply them on the next recreate.

## 3. Scheduling alert ingestion (how the app “starts” requesting data)

Ingestion does **not** run in the background by itself. Something on your network must **call the sync URL on a schedule** (every few minutes is typical).

### Prerequisites

1. **`docker compose up`** (or your stack) is running with valid **`DATTO_API_KEY`**, **`DATTO_API_SECRET`**, and **`DATTO_SITE_UIDS`**.
2. You can reach the app from the machine that will run the scheduler (e.g. `https://rmm.kraplab.work` or `http://<server-ip>:8000` if you expose port 8000 internally).

### One-off test (manual)

From any host that can reach the app:

```bash
curl -sS -w "\nHTTP %{http_code}\n" -X POST "https://rmm.kraplab.work/api/datto/sync-alerts"
```

You should get **HTTP 202** and `{"accepted":true,"sync_mode":"background",...}`. The sync runs **after** the response so HTTPS reverse proxies do not hit read timeouts on large backlogs.

Poll until `sync_running` is false, then read `last_result` (full sync JSON: `ingested`, `skipped_*`, `errors`, `alerts_row_delta`, etc.):

```bash
curl -sS "https://rmm.kraplab.work/api/datto/last-sync"
```

Live counters while a sync runs: open **`/datto-sync`** in the browser, or `GET /api/datto/sync-progress` (JSON combines `last-sync` + `live` listing/ingest metrics).

For a **synchronous** response on a small dataset (direct to uvicorn, no short proxy timeout), use `?inline=1` (not recommended through Caddy for the first large sync):

```bash
curl -sS -X POST "https://rmm.kraplab.work/api/datto/sync-alerts?inline=1&trace_limit=10"
```

Check JSON: `ok`, `ingested`, `skipped_*`, `errors`. Confirm Datto first:

```bash
curl -sS "https://rmm.kraplab.work/api/datto/status"
```

You should see `"token_ok": true` and your site UIDs listed.

### Why alerts are skipped (parse preview)

Read-only: for each list state in **sync order**, shows page-0 rows (keys, list timestamp vs cutoff, CPU/MEMORY guess from list shape, already in DB) and optionally classifies the first N merged UIDs with a real detail GET (`outcome` / `detail` matches sync logic). Full sync still walks all pages; this stays fast on page 0 only.

```bash
curl -sS "https://rmm.kraplab.work/api/datto/ingest-parse-preview?rows_per_state=30&detail_samples=15"
```

Use `site_uid=` when `DATTO_SITE_UIDS` lists multiple sites. Increase `detail_samples` (max 40) cautiously — each value triggers one Datto detail request.

### Cron (Linux) — recommended

On the **same machine as Docker** or any **internal server** that can `curl` your app URL, add a crontab entry (`crontab -e`):

```cron
# Every 5 minutes: enqueue a background sync (POST returns quickly; sync continues on the server)
*/5 * * * * curl -sS -m 60 -X POST "https://rmm.kraplab.work/api/datto/sync-alerts" >> /var/log/rmm-sync.log 2>&1
```

- **`*/5`** = every 5 minutes (use `*/3` for 3 minutes if you prefer).
- **`-m 60`** = allow up to 60 seconds for the HTTP handshake and **202** body (the Datto work continues server-side).
- Adjust the URL if you use HTTP on port 8000 only: `http://127.0.0.1:8000/api/datto/sync-alerts`.

Shorter path (same behavior):

```bash
curl -sS -X POST "https://rmm.kraplab.work/sync/alerts-from-datto"
```

### systemd timer (alternative)

If you prefer systemd over cron, use a `.service` that runs the same `curl` (or `wget`) once, plus a `.timer` with `OnCalendar=*:0/5` (every 5 minutes).

### What not to do

- Datto does **not** call your app for this flow; **your** scheduler calls **your** app.
- Do not rely on a single manual `curl` for production—use cron or systemd so ingestion continues after reboots.

## 4. Run the stack

```bash
docker compose up -d --build
```

### Database: `alerts.citrix_host` (existing PostgreSQL)

If the database was created before this column existed, add it once (safe to re-run):

```sql
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS citrix_host BOOLEAN NOT NULL DEFAULT false;
```

New environments that run `Base.metadata.create_all` on boot get the column from the model.

Caddy will:

- Listen on **80** (HTTP) and **443** (HTTPS).
- Request a certificate from Let's Encrypt for `DOMAIN` (first time may take ~30s).
- Proxy all requests to the `web` app.
- Renew certificates automatically.

### Reference: Datto-related HTTP routes

- `GET /api/datto/status` — token check and configured site UIDs.
- `GET /api/datto/alerts-count` — total rows in the **`alerts`** table (same DB the app uses).
- `GET /api/datto/ingest-diagnostic` — **troubleshooting**: first site, first open-alerts page, one detail sample; shows why rows might not ingest.
- `GET /api/datto/alerts-preview` — per-site open/resolved list counts (no DB writes).
- `POST /api/datto/sync-alerts` — full sync. Response includes **`alerts_row_delta`** (change in row count) and optional **`?trace_limit=25`** for per-UID **`status`** / **`detail`**. Same for `POST /sync/alerts-from-datto?trace_limit=25`.

### Troubleshooting: no alerts ingesting

1. **`GET /api/datto/ingest-diagnostic`** — read `summary`, `steps`, `detail`, and `http_status` on failures.
2. **`POST /api/datto/sync-alerts`** — check **`alerts_row_delta`**: if `0` while **`ingested`** is also `0`, nothing new was committed. If **`ingested` > 0** but **`alerts_row_delta`** is `0`, you may be pointed at a different database than the UI (compare with **`GET /api/datto/alerts-count`** vs SQL on your Postgres host).

   With tracing:

   ```bash
   curl -sS -X POST "https://rmm.kraplab.work/api/datto/sync-alerts?trace_limit=25" | jq .
   ```

3. **Counters** when `ingested` is 0:
   - **`skipped_list_before_cutoff`** / **`skipped_before_cutoff`** — all alerts before **`ALERT_INGEST_SINCE`** (default 2026-01-01 Eastern). Temporarily set `ALERT_INGEST_SINCE=2020-01-01` to test.
   - **`skipped_not_cpu_or_memory_type`** — **`alertContext.type`** on the detail payload must be **CPU** or **MEMORY** (see `ingest-diagnostic` for a sample `type` value).
   - **`skipped_missing_alert_timestamp`** — no parseable **`timestamp`** / **`timeStamp`** on detail **or** list row (Datto often uses **Unix milliseconds** as an integer — supported).
   - **`skipped_existing`** — alert UID already in the database.
   - **`errors`** with **`list_fetch_failed`** — wrong **`DATTO_API_URL`** for your region, bad site UID, or HTTP 401/403 (credentials / API access).
4. **Cron** — confirm the scheduler is running and the URL matches how you reach the app (`curl` from the same host the cron uses).
5. **`DATTO_SITE_UIDS`** — must be the site **`uid`** from Datto Swagger **`GET /api/v2/account/sites`**, not the site name.

6. **UI** — `/alerts` uses an **inner join** on **`devices`**. Ingest creates devices automatically; if you still see an empty list, open **`/alerts`** with **no** `timeframe` query (shows last 100 by time).

## 5. Optional: hide the app port in production

To avoid exposing the FastAPI app directly, remove the `ports` block for the `web` service in `docker-compose.yml` so only Caddy is reachable on 80/443. The app will still be reachable via Caddy.

## 6. Local / no domain

- Use `DOMAIN=localhost` and `ACME_EMAIL=admin@localhost` (or leave unset). Caddy will serve over HTTP only on port 80.
- You can still use `http://localhost:8000` for the app if you keep the `web` ports in compose.

## 7. Alternative: Nginx + Certbot

If you prefer Nginx instead of Caddy:

1. Add an `nginx` service that proxies to `web:8000`.
2. Use the `certbot/certbot` image or host certbot to obtain certs into a shared volume.
3. Configure nginx to use the certs for HTTPS and to serve `/.well-known/acme-challenge/` for HTTP-01 challenges.

Caddy is used here because it handles ACME and renewal inside the same container with no extra certbot step.
