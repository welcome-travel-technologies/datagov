# Local development

How to run the stack locally, seed a local database from production safely, and
run the tests. For how the pieces fit together see [Architecture](architecture.md).

> **The golden rule:** keep `DEBUG=True` for local work. `DEBUG` selects the
> database — `True` points the whole app (including every ETL **write**) at the
> local Docker Postgres; `False` points it at **production**. See
> [Architecture → configuration](architecture.md#configuration--environment).

---

## Quick start (Docker)

**Prerequisites:** Docker + Docker Compose.

```bash
# 1. Configure the backend (copy the sample and fill in your values)
cp backend/.env.sample backend/.env       # set DEBUG=True for local work

# 2. Start the local database
docker compose up -d db

# 3. (Optional) seed it from production — see below
pwsh scripts/seed-local-db.ps1

# 4. Build and start the whole stack
docker compose up --build
```

Then open <http://localhost>. nginx serves the SPA and proxies the API. Always run
Compose from the **repo root** with the root `docker-compose.yml` (there is also a
`backend/docker-compose.yml`; use `-f docker-compose.yml` to be explicit if your
shell's working directory is `backend/`).

The first `web` boot runs `migrate`, `createcachetable`, and `collectstatic`
automatically (see [`backend/entrypoint.sh`](../backend/entrypoint.sh)).

---

## Running the apps separately (no Docker)

The frontend dev server proxies `/api/*` to Django, so the two run side by side:

```bash
# Backend (from backend/app)
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 8000

# Frontend (from frontend/)
npm install
npm run dev          # http://localhost:3000, proxies /api -> :8000
```

You also need the **worker** for any ETL or AI-assistant work (both run on
Django-Q2):

```bash
# from backend/app
python manage.py qcluster
```

Auth uses the Django session: the SPA calls `GET /api/me/`, `POST
/api/auth/login/`, `POST /api/auth/logout/`. Because Next proxies `/api/*`, the
session and CSRF cookies flow on one origin — no CORS setup needed.

---

## Seeding a local DB from production

The app reads integration credentials (Power BI `client_secret`, dbt
`github_token`, …) **and** writes ETL results into the *same* Postgres. To run the
ETL without any risk to production, point the whole stack at a **local Docker
Postgres seeded with a dump of production**. Production is only ever *read* — once,
during the seed.

### How it's wired

- **`backend/.env`** — the active `DB_*` point at the local `db` container
  (`DB_HOST=db`). Production lives under `PROD_DB_*`, used *only* by the seed
  script.
- **`docker-compose.yml`** — the `db` service (postgres:18); `web`/`worker` wait
  for it to be healthy.
- **`settings.py`** — `DB_SSLMODE` (local Postgres has no SSL) and
  `SECURE_COOKIES` (so login works over `http://localhost`) are env-driven;
  production defaults are unchanged.

### One-time setup

```powershell
docker compose up -d db                # 1. start the local Postgres
pwsh scripts/seed-local-db.ps1         # 2. dump production into it
docker compose up --build              # 3. start the whole stack
```

[`scripts/seed-local-db.ps1`](../scripts/seed-local-db.ps1) reads `PROD_DB_*` and
`DB_*` from `backend/.env`, runs `pg_dump` against production inside a throwaway
`postgres:18` container (no host `pg_dump` needed), writes `.seed/prod.sql`, and
restores it into the local `db` container. Then open <http://localhost> and run the
ETL (UI or `python manage.py run_source <id>`) — every write goes to the local
copy.

### Refreshing & resetting

- Re-run `pwsh scripts/seed-local-db.ps1` any time to pull fresh credentials/data.
- To wipe the local DB entirely: `docker compose down -v` (drops the `pgdata`
  volume), then seed again.

### Notes

- Both the local `db` image and the dump client are pinned to **postgres:18**
  (production is Postgres 18). If production is upgraded to a newer major, bump
  `$PG_IMAGE` in the script and the `db` image tag to match — `pg_dump` refuses to
  dump from a newer server.
- `.seed/prod.sql` is git-ignored — it contains real secrets.
- The seed overwrites the local superuser, and re-seeding can hit a cache-table
  primary-key error; if so, `docker compose down -v` and re-seed for a clean slate.

---

## Tests

```bash
# Backend (from backend/app)
pytest

# Frontend (from frontend/)
npm test
npm run typecheck
```

The backend suite lives in
[`backend/app/catalog/tests/`](../backend/app/catalog/tests/) (pytest-django) and
covers the ETL, bridge matching, column lineage, chat, governance, serializers,
and views. The frontend has Vitest unit tests beside the pure logic in `lib/`.

---

## Useful management commands

Run from `backend/app` with `python manage.py <cmd>`:

| Command | Purpose |
|---|---|
| `run_source <id>` | Run one integration source synchronously |
| `test_sources [--source-id N]` | Connectivity-test active sources |
| `run_workflow_final [--organization-id N]` | The cross-tool bridge + stats step |
| `rebridge [--organization-id N]` | Rebuild only the dbt↔Power BI bridge edges |
| `load_data` / `load_dbt_data` | Load transform CSVs into the catalog |
| `chat_repl [--org-id N]` | Interactive AI-assistant REPL |
| `clean_database` | Reset catalog data |

See [ETL](etl.md) and [Assistant](assistant.md) for details.
