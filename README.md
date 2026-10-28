# DataFlow ETL Studio

[![tests](https://github.com/Manishjangir01/dataflow-etl-studio/actions/workflows/tests.yml/badge.svg)](https://github.com/Manishjangir01/dataflow-etl-studio/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

> No-code ETL in the browser: extract from REST APIs or files, clean and validate the data, load it into CSV, Parquet, SQLite or PostgreSQL.

*Built with guidance from an AI mentor.*

A no-code ETL web application: point it at a **REST API** or upload a **CSV / JSON / Parquet** file,
clean and transform the data with a visual builder, validate it with data-quality rules, and load it
into **CSV, Parquet, SQLite or PostgreSQL** - on demand or on a schedule.

Everything is real: extraction, transformation, validation and loading run against your data, every
load is verified by re-counting rows, and a run is only marked successful when that check passes.

---

## Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Installation on Windows](#installation-on-windows)
5. [Environment configuration](#environment-configuration)
6. [SQLite local setup](#sqlite-local-setup)
7. [PostgreSQL setup](#postgresql-setup)
8. [Running the application](#running-the-application)
9. [Build and run your first pipeline](#build-and-run-your-first-pipeline)
10. [Configure an example API](#configure-an-example-api)
11. [Scheduling](#scheduling)
12. [Running the tests](#running-the-tests)
13. [Docker](#docker)
14. [Command line](#command-line)
15. [Troubleshooting](#troubleshooting)
16. [Security considerations](#security-considerations)
17. [Known limitations](#known-limitations)
18. [Future enhancements](#future-enhancements)

---

## Features

**Sources**
- REST APIs with GET / POST, headers, query parameters, optional JSON body
- Authentication: none, API key (header or query parameter) or Bearer token
- Pagination: page number, offset/limit, cursor, next-page URL (response field or HTTP `Link` header)
- Response handling: JSON arrays and nested objects, automatic detection of the field that holds the records (or pick it from a list), nested objects flattened to `parent.child` columns
- Timeouts, retries with exponential back-off, HTTP 429 rate limits with `Retry-After`, response size limit, clear errors for HTTP failures and malformed responses
- File uploads: CSV/TSV (delimiter and encoding auto-detection), JSON, JSON Lines, Parquet; bundled messy sample datasets

**Preview and data quality**
- Record / column counts, raw data sample, detected column types (integer, float, boolean, datetime, email, text)
- Missing values, duplicate rows, **invalid values** (e.g. `"thirty"` in a numeric column) with examples, quality score
- Choose which columns to keep

**Transformation builder** (13 step types, reorder / enable / disable / edit, live preview)
- Remove duplicates, handle missing values (drop, fixed value, mean, median, most frequent, forward/backward fill)
- Rename columns, standardize column names (`Order Date` → `order_date`), select / reorder / remove columns
- Change data types, parse and standardize dates, trim whitespace, text case, replace values
- Filter rows (simple conditions or expressions), calculated columns
- A safe expression language (no `eval`): `round(price * qty * 1.18, 2)`, `"adult" if age >= 18 else "minor"`, `concat(first, " ", last)` …
- Errors name the step number, the step type and the column

**Validation rules**
- Required columns, not empty, unique (single or composite), numeric range, allowed values, regex pattern, valid data type, row count, and business rules as expressions
- Each rule either **rejects** bad rows (the rest is loaded, rejected rows are downloadable) or **stops** the run before anything is loaded

**Destinations**
- CSV and Parquet exports (atomic writes into `data/output`)
- SQLite (`data/databases/*.db`) and PostgreSQL (connection fields with an encrypted password, or a URL from an environment variable)
- Strategies: **replace** (via a staging table - a failed load keeps the old table), **append**, **upsert** on one or more key columns (a unique index is created when needed)
- Parameterized SQL, validated table names, one transaction per load, verified row counts, warnings and confirmation before destructive replaces

**Operations**
- Pipeline workflow with step-by-step status, duration and extracted / transformed / rejected / loaded counts
- Execution history, per-run reports, downloadable logs (secrets redacted), retry (optionally re-using the previous run's API data)
- Schedules (interval, daily, weekly, cron) with pause / resume, run by APScheduler in the app or in a separate worker
- No overlapping runs of the same pipeline (database lock); runs interrupted by a crash are detected and marked failed
- Dashboard with total / successful / failed runs, records processed and recent activity
- CLI for automation and an example Airflow DAG

---

## Architecture

```mermaid
flowchart LR
    subgraph UI["Streamlit UI  (app.py + pages/)"]
        W[Create Pipeline wizard]
        P[Pipeline Runs / Data Quality / Overview]
    end
    subgraph Core["src/"]
        C[connectors<br/>API client, auth,<br/>pagination, retries]
        X[extraction<br/>file readers,<br/>JSON flattening]
        T[transformations<br/>steps + safe expressions]
        V[validation<br/>profiler + rule engine]
        L[loading<br/>CSV / Parquet /<br/>SQLite / PostgreSQL]
        O[orchestration<br/>runner, locks,<br/>scheduler, worker]
        S[storage<br/>SQLAlchemy models]
        SEC[security<br/>URL guard, Fernet,<br/>redaction]
    end
    API[(REST API)] --> C
    FILE[(Uploaded file)] --> X
    W --> O
    P --> O
    O --> C & X --> T --> V --> L
    L --> DEST[(CSV / Parquet /<br/>SQLite / PostgreSQL)]
    O --> S --> META[(Metadata DB<br/>SQLite or PostgreSQL)]
    SEC -.-> C & L & O
    SCHED[APScheduler<br/>embedded or worker] --> O
    CLI[CLI / Airflow] --> O
```

Text version:

```
 Streamlit UI ──┐            ┌── APScheduler (embedded or worker) ──┐   CLI / Airflow
                ▼            ▼                                       ▼
            ┌──────────────────────── PipelineRunner ─────────────────────────┐
            │  lock → Extract → Profile → Transform → Validate → Load & verify │
            └───────┬────────────┬──────────┬────────────┬────────────┬───────┘
                    │            │          │            │            │
              connectors    profiler   transformations  rules      loading
              (URL guard,                (safe expr)              (transactions,
               secrets)                                            staging tables)
                    │                                                 │
              REST API / file                       CSV / Parquet / SQLite / PostgreSQL
                                    run history, logs, locks, settings
                                    → metadata DB (SQLite or PostgreSQL)
```

### Project layout

```
dataflow-etl-studio/
├── app.py                      Streamlit entry point and navigation
├── pages/                      Overview, Create Pipeline, Data Sources, Transformation Builder,
│                               Destinations, Pipeline Runs, Data Quality Reports, Settings
├── src/
│   ├── connectors/             API source models, API client, extractor factory
│   ├── extraction/             CSV/JSON/Parquet readers, JSON → table normalisation
│   ├── transformations/        transformation steps, engine, safe expression evaluator
│   ├── validation/             data-quality profiler, validation rule engine
│   ├── loading/                destination models, SQL loader, file loader
│   ├── orchestration/          pipeline config, runner, scheduler, worker
│   ├── storage/                SQLAlchemy engine, ORM models, repository
│   ├── security/               URL/SSRF guard, encrypted secrets, redaction
│   ├── utils/                  settings, logging, exceptions
│   ├── ui/                     shared Streamlit components
│   └── cli.py                  command line interface
├── tests/                      pytest suite (mocked APIs, temporary databases)
├── data/samples/               sample datasets (customers.csv, orders.json, events.jsonl, products.parquet)
├── data/{uploads,output,databases,runs}/   runtime data (git-ignored)
├── logs/                       application and per-run logs (git-ignored)
├── config/                     generated encryption key if none is configured (git-ignored)
├── examples/                   Airflow DAG, sample generator
├── docker/postgres-init/       creates the "warehouse" database in Docker
├── scripts/                    Windows PowerShell helpers
├── requirements.txt, .env.example, .gitignore, Dockerfile, docker-compose.yml
```

---

## Prerequisites

- **Windows 10 or 11** (macOS / Linux work the same way with the shell commands noted below)
- **Python 3.12 or 3.13, 64-bit** - from [python.org](https://www.python.org/downloads/windows/). During setup tick *Add python.exe to PATH*. The Windows launcher `py` is installed with it.
- About 1 GB of free disk space for the virtual environment
- Optional: **PostgreSQL 14 or newer** (for the PostgreSQL destination or metadata database)
- Optional: **Docker Desktop** (to run everything with `docker compose`)

Check your Python version in PowerShell:

```powershell
py --list
py -3.12 --version
```

---

## Installation on Windows

Open **PowerShell** in the project folder (in File Explorer: *Shift + right-click → Open PowerShell window here*).

### Quick setup (script)

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned   # once, allows local scripts
.\scripts\setup_windows.ps1
```

The script creates the virtual environment, installs the dependencies, creates `.env` with a fresh
encryption key and initialises the database. Then start the app with `.\scripts\run_app.ps1`.

### Manual setup (step by step)

**1. Create a virtual environment**

```powershell
py -3.12 -m venv .venv
```

**2. Activate it** (repeat in every new terminal)

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell says running scripts is disabled, run once:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.
In the classic Command Prompt (`cmd.exe`) use `.venv\Scripts\activate.bat` instead.
On macOS / Linux: `python3.12 -m venv .venv` and `source .venv/bin/activate`.

**3. Install the dependencies**

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**4. Create your configuration**

```powershell
Copy-Item .env.example .env
python -m src.cli generate-key
```

Open `.env` in a text editor and paste the printed key after `DATAFLOW_SECRET_KEY=`.

**5. Initialise the database**

```powershell
python -m src.cli init-db
```

---

## Environment configuration

All settings are environment variables, usually placed in `.env` (never commit that file - it is in
`.gitignore`). The most important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DATAFLOW_DATABASE_URL` | `sqlite:///data/dataflow.db` | Metadata database (pipelines, run history, logs). SQLite or `postgresql+psycopg://…` |
| `DATAFLOW_SECRET_KEY` | auto-generated in `config/secret.key` | Fernet key that encrypts API keys, tokens and DB passwords |
| `DATAFLOW_TIMEZONE` | `UTC` | Default time zone for schedules, e.g. `Asia/Kolkata` |
| `DATAFLOW_EMBEDDED_SCHEDULER` | `true` | Run schedules inside the web app; set `false` when using the worker |
| `DATAFLOW_ALLOWED_DOMAINS` | empty (= any public domain) | Comma-separated allow-list of API domains |
| `DATAFLOW_ALLOW_HTTP` | `false` | Allow plain `http://` API URLs |
| `DATAFLOW_USE_ENV_PROXY` | `false` | Send API calls through `HTTP(S)_PROXY` |
| `DATAFLOW_MAX_UPLOAD_MB` / `DATAFLOW_MAX_RESPONSE_MB` | 200 / 50 | Size limits for uploads and API responses |
| `DATAFLOW_LOCK_TIMEOUT_MINUTES` | 120 | After this time without a heartbeat a run lock is considered stale |
| `DATAFLOW_DATA_DIR`, `DATAFLOW_LOGS_DIR` | `data`, `logs` | Where runtime files are written |

Values are validated at start-up; a wrong value shows a clear message in the browser.
Keep `.streamlit/config.toml`'s `maxUploadSize` in line with `DATAFLOW_MAX_UPLOAD_MB`.

---

## SQLite local setup

Nothing to install - SQLite ships with Python.

- The **metadata database** is `data/dataflow.db` (created automatically).
- **SQLite destinations** are files in `data/databases/` (e.g. `warehouse.db`); tables are created on the first load.
- Browse loaded tables in the app under **Destinations → SQLite databases**, or open the `.db` file with
  [DB Browser for SQLite](https://sqlitebrowser.org/).

---

## PostgreSQL setup

### Install on Windows

1. Download the installer from <https://www.postgresql.org/download/windows/> (EDB build) and install it.
   Remember the password you choose for the `postgres` superuser. Keep port `5432`.
2. Open **SQL Shell (psql)** from the Start menu (or `psql -U postgres` in a terminal) and create a user and
   databases for DataFlow (replace the password with your own):

```sql
CREATE USER etl_user WITH PASSWORD 'choose-a-strong-password';
CREATE DATABASE warehouse OWNER etl_user;   -- where pipelines load data
CREATE DATABASE dataflow  OWNER etl_user;   -- optional: metadata database
```

Alternatively run PostgreSQL in Docker:
`docker run -d --name pg -e POSTGRES_PASSWORD=choose-a-strong-password -p 5432:5432 postgres:16`.

### Use PostgreSQL as a destination

In the wizard's **Destination** step choose *PostgreSQL database*, then either:

- **Enter connection details** - host `localhost`, port `5432`, database `warehouse`, user `etl_user` and
  the password. The password is encrypted immediately and never shown again. Use *Test connection*.
- **Connection URL from an environment variable** - add to `.env`:
  `WAREHOUSE_PG_URL=postgresql+psycopg://etl_user:choose-a-strong-password@localhost:5432/warehouse`
  and enter `WAREHOUSE_PG_URL` as the variable name (restart the app after editing `.env`).

### Use PostgreSQL for the metadata database (recommended for servers)

```
DATAFLOW_DATABASE_URL=postgresql+psycopg://etl_user:choose-a-strong-password@localhost:5432/dataflow
```

Then run `python -m src.cli init-db`. Tables are created automatically.

---

## Running the application

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py
```

The browser opens at <http://localhost:8501>. Stop the app with **Ctrl+C** in the terminal.
To use another port: `streamlit run app.py --server.port 8502`.

---

## Build and run your first pipeline

1. Open **Create Pipeline**.
2. **Source** → *Use a sample dataset* → `customers.csv` → **Load sample**.
   (Or upload your own CSV / JSON / Parquet file, or choose *REST API*.)
3. **Preview** - look at the quality report: 1 duplicate row, missing values, an invalid age (`thirty`),
   an invalid date and an invalid e-mail. Optionally remove columns.
4. **Transform** - add steps and watch the preview update:
   - *Trim whitespace* (all text columns)
   - *Remove duplicate rows*
   - *Standardize text case* → `country`, Title Case
   - *Change data types* → `age` = integer, `lifetime_value` = float
   - *Parse and standardize dates* → `signup_date`, output *Date*
   - *Create calculated column* → `value_band` = `"high" if lifetime_value >= 1000 else "normal"`
5. **Validate** - add rules:
   - *Value is not empty* → `email` → *Reject the row*
   - *Matches pattern* → `email` → preset *Email address* → *Reject the row*
   - *Number within range* → `lifetime_value` minimum `0` → *Reject the row*
6. **Destination** - *SQLite database*, file `warehouse.db`, table `customers`, strategy *Append*
   (or *Upsert* with key `customer_id`) → **Save destination** (*Test connection* is optional).
7. **Review & Run** - name the pipeline, optionally add a schedule, click **Save and run now**.
   You will see each step's status, the duration and the extracted / transformed / rejected / loaded counts.
8. Open **Pipeline Runs** for the history, full reports, the log download and **Retry**;
   **Data Quality Reports** for quality trends and rejected rows; **Destinations** to look at the loaded table.

---

## Configure an example API

### Public API without authentication - JSONPlaceholder

JSONPlaceholder is a free fake REST API that returns nested JSON.

| Field | Value |
|---|---|
| API endpoint URL | `https://jsonplaceholder.typicode.com/users` |
| HTTP method | GET |
| Authentication | None |
| Pagination | No pagination |

Tick *I confirm I am authorized…*, click **Test connection**, then **Extract data**. The nested
`address` and `company` objects become columns such as `address.city` and `company.name`.
Add *Standardize column names* in the transformations before loading into a database.

Paginated example: URL `https://jsonplaceholder.typicode.com/posts`, pagination *Page number*,
page parameter `_page`, page size parameter `_limit`, page size `20`, maximum pages `10`.

### API with a token - GitHub REST API

| Field | Value |
|---|---|
| API endpoint URL | `https://api.github.com/repos/pandas-dev/pandas/issues` |
| Authentication | Bearer Token, secret source *Environment variable* `GITHUB_TOKEN` |
| Query parameters | `state` = `all`, `per_page` = `100` |
| Pagination | *Next-page URL*, leave the response field empty (uses the `Link` header), max pages `5` |

Create a fine-grained personal access token on GitHub, add `GITHUB_TOKEN=...` to `.env` and restart.
(Unauthenticated calls also work but GitHub limits them to 60 requests per hour - DataFlow then
reports the HTTP 403 / 429 response clearly.)

### Restricting which APIs can be called

Under **Settings → API security** list the domains that may be called (e.g. `api.github.com`).
Localhost, private networks and cloud metadata addresses are always blocked, whatever you configure.

---

## Scheduling

Add a schedule in the wizard's last step or under **Pipeline Runs → Change schedule**:
*every N minutes*, *daily at HH:MM*, *weekly on chosen days* or a *cron expression*, in any time zone.
Pause and resume with one click; the next run time is shown.

**Two ways to run schedules**

| Mode | How | When to use |
|---|---|---|
| Embedded (default) | `DATAFLOW_EMBEDDED_SCHEDULER=true`; the scheduler runs inside `streamlit run app.py` | Local use on one PC |
| Worker | set it to `false` and run `python -m src.orchestration.worker` (or `.\scripts\run_worker.ps1`) in a second terminal | Servers, Docker, or when the UI restarts often |

Run **only one** of them. Schedule changes made in the UI reach a running worker within 30 seconds.

**Limitations of the in-process scheduler** (also explained on the Settings page)

- Schedules fire only while the app / worker process is running (not while the PC sleeps or is off).
  A run missed by more than 5 minutes is skipped; several missed runs are merged into one.
- Single machine only; no distributed execution, alerting or dependencies between pipelines.
- A run that is still active when the next scheduled run is due causes that run to be recorded as *skipped*
  (overlapping runs of the same pipeline are prevented by a database lock).

**Path to Apache Airflow.** Every pipeline can be run with `python -m src.cli run --pipeline-id N`
(exit code 0 = success, 1 = failure), and every such run is recorded in the same history.
`examples/airflow_dag.py` is a ready DAG that runs pipelines in order with retries:
run Airflow on Linux / WSL2 / Docker, copy the DAG into `dags/`, set the `dataflow_home` and
`dataflow_python` Airflow Variables, use PostgreSQL as the metadata database and set
`DATAFLOW_EMBEDDED_SCHEDULER=false`.

---

## Running the tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

The suite (160+ tests) uses mocked HTTP responses (`responses`) and temporary SQLite databases - no
internet connection or live API is needed. It covers API extraction, every pagination style,
authentication, invalid responses and SSRF blocking, null / duplicate handling, type conversion,
transformation rules, safe expressions, data-quality validation, database and file loading,
append / replace / upsert, transactional rollback, pipeline runs, failed-run recovery and retry,
overlapping-run prevention, scheduling, the CLI, secret redaction and the Streamlit pages themselves.

Run the database loading tests against a real PostgreSQL as well:

```powershell
$env:DATAFLOW_TEST_POSTGRES_URL = "postgresql+psycopg://etl_user:choose-a-strong-password@localhost:5432/warehouse"
python -m pytest tests/test_loading.py tests/test_pipeline_runner.py
```

(The tests create and drop their own randomly named tables.)

Useful options: `python -m pytest -x` (stop at first failure), `python -m pytest tests/test_transformations.py`,
`python -m pytest -k upsert`.

---

## Docker

Runs PostgreSQL, the web app and the scheduler worker:

```powershell
Copy-Item .env.example .env
# In .env set POSTGRES_PASSWORD and DATAFLOW_SECRET_KEY (python -m src.cli generate-key)
docker compose up -d --build
```

Open <http://localhost:8501>. Pipeline data and logs are stored in Docker volumes; the metadata database is
the `dataflow` PostgreSQL database. For a PostgreSQL destination inside Docker choose
*Connection URL from an environment variable* and enter `WAREHOUSE_PG_URL` (already set by Compose, pointing
to the `warehouse` database).

```powershell
docker compose logs -f app worker     # follow logs
docker compose down                   # stop (data is kept)
docker compose down -v                # stop and DELETE all data volumes
```

---

## Command line

```powershell
python -m src.cli generate-key                 # new encryption key
python -m src.cli init-db                      # create / check the metadata database
python -m src.cli list                         # pipelines and their IDs
python -m src.cli run --pipeline-id 1          # run now (exit code 0 / 1)
python -m src.cli run --retry-of 12 --cached   # retry run 12 re-using its API data
python -m src.cli runs --limit 20              # recent runs
python -m src.cli purge --days 90              # delete run history older than 90 days
python -m src.cli worker                       # start the scheduler worker
```

The CLI also works with **Windows Task Scheduler** if you prefer it over the built-in scheduler:
program `C:\path\to\project\.venv\Scripts\python.exe`, arguments `-m src.cli run --pipeline-id 1`,
start in `C:\path\to\project`.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `py` / `python` is not recognized | Reinstall Python with *Add python.exe to PATH*, or use the full path, e.g. `C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe` |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `pip install` fails building `pyarrow` / `pandas` | You are on 32-bit Python or a Python version without wheels; install 64-bit Python 3.12 or 3.13 |
| `DATAFLOW_TIMEZONE ... is not a valid IANA time zone` | Use names like `Asia/Kolkata`; make sure `tzdata` is installed (`pip install -r requirements.txt`) |
| `A saved secret could not be decrypted` | The encryption key changed. Restore the old `DATAFLOW_SECRET_KEY` / `config/secret.key`, or re-enter the secret |
| API: *resolves to a private … address and is blocked* | Intended: only public APIs are allowed. Local test APIs cannot be used as sources |
| API: *is not in the list of allowed API domains* | Add the domain under Settings → API security (or `DATAFLOW_ALLOWED_DOMAINS`) |
| API: HTTP 401 / 403 | Check the key/token, the key name and whether it goes in a header or query parameter |
| API: *response is not valid JSON* | The URL returns HTML (a login page or docs) - use the JSON endpoint |
| API: records not found / wrong rows | Use *Test connection* and pick the right *Field containing the records* |
| Behind a corporate proxy, API calls time out | Set `DATAFLOW_USE_ENV_PROXY=true` and `HTTPS_PROXY=http://proxy:port` in `.env` |
| *Cannot overwrite file … the file is locked* | Close the CSV in Excel and retry the run |
| *The table has no column(s): …* | The target table has a different structure: rename/remove columns in the transformations, or use Replace |
| *Cannot add a unique index … duplicate keys* | The existing table has duplicate keys; clean them or choose another upsert key |
| PostgreSQL: *password authentication failed* | Wrong user/password; for env URLs, special characters in the password must be URL-encoded (`@` → `%40`) |
| PostgreSQL: *connection refused* | The server is not running or listens on another port; check the PostgreSQL service in `services.msc` |
| *The pipeline is already running* | Wait for the active run; a crashed run's lock is released after `DATAFLOW_LOCK_TIMEOUT_MINUTES` or on restart |
| Port 8501 is busy | `streamlit run app.py --server.port 8502` |
| Scheduled runs do not happen | The app or worker must be running; check Settings → Scheduler and `logs/dataflow.log` / `logs/worker.log` |

Logs: `logs/dataflow.log` (web app), `logs/worker.log` (scheduler worker), `logs/cli.log` (command line) and
`logs/runs/run_<id>.log` (each run). All of them are redacted.

---

## Security considerations

- **Secrets** (API keys, tokens, database passwords) are encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
  as soon as they are typed, stored only as ciphertext, never displayed again, and can alternatively be read
  from environment variables. Back up your key - `config/secret.key` is git-ignored.
- **Redaction**: every decrypted secret is registered and masked in logs, error messages, run reports and
  downloads; credential patterns (`Authorization: Bearer …`, `?api_key=…`, `password=…`, passwords in
  database URLs) are masked even if unknown. Headers / query parameters that look like credentials are
  rejected so secrets always go through the encrypted path.
- **SSRF protection**: API URLs must be `https` (http optional), may not contain credentials, and every
  resolved address must be public. The check is repeated on every socket connection, which also blocks
  DNS rebinding and redirects to internal hosts. Credentials are never sent to another host
  (cross-host redirects and pagination links are refused). An optional domain allow-list restricts sources further.
  Note: with `DATAFLOW_USE_ENV_PROXY=true` the per-connection IP check is the proxy's responsibility.
- **SQL safety**: validated identifiers, quoting by SQLAlchemy, bound parameters for all values,
  one transaction per load with verification before commit.
- **Expressions** are parsed with a whitelist (no `eval`, attribute access, imports or indexing).
- **Files** are only read from `data/uploads` and written to `data/output` / `data/databases`; names are sanitised.
- **The web UI has no login.** Run it on `localhost` or behind an authenticating reverse proxy / VPN.
  Anyone who can open the UI can run pipelines and use the saved (encrypted) credentials.
- Database destinations may point at private network hosts (that is where databases live); only API
  sources are restricted.

---

## Known limitations

- No user accounts / roles in the UI (see Security).
- Data is processed in memory with pandas; very large sources (many millions of rows) need more RAM.
  Use the *Maximum records per run* and pagination limits for big APIs.
- Upsert is available for SQLite and PostgreSQL; files support replace and append.
- Appending to an existing table requires the new data's columns to exist in the table (no automatic schema migration).
- Replace recreates the table from the data, so indexes, constraints or grants added manually to that table are not kept.
- OAuth 2.0 flows (client credentials, refresh tokens) are not built in; use a pre-issued bearer token.
- Regular expressions in validation rules run with Python's `re` module; avoid pathological patterns on huge text columns.
- The Docker image and Compose file were validated (`docker compose config`) but the image build itself
  needs internet access to Docker Hub.

---

## Future enhancements

- User authentication and roles; audit log of configuration changes
- More sources and destinations: databases (MySQL, SQL Server), S3 / Azure Blob, Google Sheets, BigQuery, Snowflake
- OAuth 2.0 client-credentials and token refresh
- Incremental extraction (watermarks such as `updated_since`) and change data capture
- Chunked / out-of-core processing (DuckDB or Polars) for very large datasets
- Schema evolution (add new columns on append) and Alembic migrations for the metadata database
- E-mail / Slack / Teams notifications on failure
- Pipeline dependencies and a DAG view; export pipelines as Airflow DAGs automatically
- Data lineage and column-level documentation
- Import / export of pipeline definitions as JSON

---

Built with Python, Streamlit, pandas, requests, SQLAlchemy, psycopg, pydantic, APScheduler and pytest.

---

## License

MIT, see [LICENSE](LICENSE).
