# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django 6.0 project for a people-counting middleware. Early stage: one app (`core`) with a placeholder `EventLog` model exposed via Django admin. No views/URLs/tests implemented yet. No `requirements.txt`, README, or lint config exists.

- `server/` — project config (settings, root URLconf, WSGI/ASGI). Currently SQLite at `db.sqlite3` (committed); target is PostgreSQL.
- `core/` — main app. The current `EventLog` (`event_type`, `description`, `logged_at`) is scaffolding and does not match the spec below.
- Python 3.12 is installed at `%LOCALAPPDATA%\Programs\Python\Python312\`. PostgreSQL 16 is installed locally.
- Reference docs in repo root: `PRD - People Counting Middleware.pdf`, flowchart PNG, `Level3 Open API-*.pdf` (ZK API), `Dokumentasi_API_Integrasi_WO_IoT_Algospection*.pdf` (Work Order API). Extract text with `pdftotext -layout`.

## Product Spec (PRD + decisions confirmed with user, 2026-10-01)

Middleware that polls ZK people-counting sensors and creates a Work Order in Algospection when a device's traffic reaches a threshold. Stack: Django, Python 3.12, PostgreSQL, **APScheduler**. Everything (thresholds, interval, endpoints) is managed via Django Admin. Timezone: `Asia/Jakarta` (ZK `eventTime` has no TZ and is WIB).

**First run (baseline):** fetch page 1 of each device's events and store them without counting, so historical events (40k+) never trigger a Work Order.

**Scheduled job — interval configurable in Admin (default 1 minute), per device:**
1. Get ZK token: `POST /global/open/api/v1/oauth2/token` with JSON `{client_id, client_secret, grant_type: "client_credentials"}` → `data.access_token`. Success when `code == "00000000"`.
2. Fetch events: `GET /zt/open/api/v1/event/page?page=N&pageSize=10&deviceId=<id>&type=3` with `Authorization: Bearer <token>`. Results are newest first; keep paging until an already-stored `id` is found. On 401/expired token → refresh and retry once in the same cycle.
3. Log every ZK request (token + events, success or failure) to `SensorLog`.
4. Store new events in `EventLog` (dedupe by ZK `id`). Mapping: `id`→`id`, `eventTime`→`time`, `eventLogVO.trackId/height/eventType/recognitionTarget`→`track_id/height/event_type/recognition_target`.
5. Count: each new event with `eventType == "in"` and `recognitionTarget == "Cross Line"` adds +1 to `DeviceList.current_count`. Ignore `out`, `passby`, `passby and in`, `turnback` (changed 2026-10-01: only `in` counts). Real values are lowercase and "Cross Line" has a space. The count is per WIB day by event `eventTime`: the first counted event on a newer date resets `current_count` to 0 first (leftover from the previous day is discarded, no Work Order); `DeviceList.count_date` tracks the day.
6. If `current_count < maximum_trigger` → stop; repeat next interval.
7. If `current_count >= maximum_trigger` → POST Work Order to the notification endpoint, log to `NotificationLog` (no retry), then reset `current_count` to 0 even if the POST failed.

**Models:**
- `Endpoint`: `id`, `url`, `head` (JSON), `body` (JSON), plus a type field: `token` / `event` / `notification`. Holds 3 rows: ZK token, ZK event, Work Order target.
- `DeviceList`: `id` (ZK device ID; initial `2069691213314072577`), `type`, `current_count` (default 0), `maximum_trigger` (default 10, editable in Admin).
- `EventLog`: `id` (ZK event id), `metadata_id`, `time`, `track_id`, `event_type`, `recognition_target`, `height`, `device` FK.
- `SensorLog`: log of ZK requests — `id` (UUID), `status`, `response` (JSON), `time`, `endpoint_url`, `device` FK.
- `NotificationLog`: log of Work Order POSTs — `id` (UUID), `time`, `device` FK, `endpoint_url`, `body`, `head`, `response` (JSON), `response_status`.
- All FKs use `on_delete=CASCADE`.

**Work Order target (Algospection, `https://issid-inspection.com/api_iot.php`):** not accessible yet, so build a **dummy API** in a separate Django app (e.g. `dummy_wo`, `POST /dummy/api_iot.php`). It validates the static `token` and required fields, stores requests in its own table (visible in Admin), and replies like the Algospection doc (`{"status":"success","message":"Work Order berhasil dibuat","wo_id":...}`, or 401 `{"status":"error",...}`). The notification `Endpoint` points to the dummy for now. Body uses the doc example values but with the dummy token, `INSTANCE: "prod"`, `LOC_ID: "GRAHA ISS BINTARO"`, `ASSET_ID: "SPACE-GRAHAISS-0020"`, `REQ_TYP: "CHECK-ROOM-TOILET"`, `REQ_DESC: "Toilet Traffic Counter"`.

**Secrets:** ZK client ID/secret and DB credentials go in `.env` (never committed).

## Commands

Use the venv: `.venv\Scripts\python` (deps in `requirements.txt`). Config comes from `.env` (template: `.env.example`). PostgreSQL 16 runs locally on 5432, DB `people_counting`.

- Web + Admin + dummy Work Order API: `python manage.py runserver`
- Scheduler (separate terminal, keeps running): `python manage.py run_scheduler`
- One sync cycle manually: `python manage.py sync_once`
- Backfill history (no counting, no Work Order): `python manage.py backfill_events --date YYYY-MM-DD [--device ID]`
- Migrations: `python manage.py makemigrations` then `python manage.py migrate` (`core/0002` seeds endpoints, the device and scheduler config)
- All tests: `python manage.py test`; single test: `python manage.py test core.tests.SyncDeviceTests.test_counts_only_in_cross_line`
- Admin user: `python manage.py createsuperuser`

## Docker

- `docker-compose.yml` (production-style): `db` (postgres:16), `web` (Gunicorn; runs migrate + collectstatic on start via `docker/entrypoint.sh` when `RUN_MIGRATIONS=1`), `scheduler` (`run_scheduler`), `nginx` (port `NGINX_PORT`, serves `/static/`). Config from `.env`; compose overrides `DB_HOST=db`.
- Dev: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` (runserver on :8000, source mounted, Postgres on host :5433).
- The dummy Work Order URL inside Docker is `http://web.internal:8000/dummy/api_iot.php` (dotted network alias, because Admin's URLField rejects bare `web`). `web.internal` must be in `DJANGO_ALLOWED_HOSTS`.
- Only one `scheduler` container may run, otherwise events are processed twice.

## Code Layout

- `core/services.py` — the whole sync engine (`ZKClient`, `fetch_new_events`, `sync_device`, `dispatch_work_orders`).
- `core/management/commands/run_scheduler.py` — APScheduler loop; re-reads `SchedulerConfig` every 30s to apply interval changes.
- `dummy_wo/` — local stand-in for Algospection `api_iot.php`. Uses its own token (`DUMMY_WO_TOKEN`), deliberately different from the real one.
- Tests mock `requests`; they never call ZK or Algospection.

## Git Convention

- Every commit message and Pull Request title MUST start with an action prefix:
  - `[FEAT]` — new feature development
  - `[FIX]` — bug fixing
  - `[BUILD]` — project initialization / initial setup
- Format: `[ACTION] short description in imperative mood`
- Examples:
  - `[FEAT] Add user login endpoint`
  - `[FIX] Resolve null pointer on checkout page`
  - `[BUILD] Initialize project structure and dependencies`
- Never push or create a PR without one of these prefixes.
- Use exactly one prefix per commit/PR. If changes span more than one type, split them into separate commits/PRs.
- If the change type is unclear, ask the user before committing or pushing.
