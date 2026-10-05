# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django 6.0 project for a people-counting middleware. The sync engine, scheduler, Admin and dummy Work Order API are implemented and tested. No README or lint config exists.

- `server/` — project config (settings, root URLconf, WSGI/ASGI). PostgreSQL only; settings read everything from `.env` via python-dotenv (`DJANGO_SECRET_KEY` is required).
- `core/` — main app: models, sync engine, Admin, management commands.
- `dummy_wo/` — local stand-in for the Algospection Work Order API.
- `washroom/` — REST API (DRF) for the ZK side to send washroom sensor data and ratings, on `core.DeviceList`. See "Washroom API" below.
- `dashboard/` — the public dashboard (team-built). See "Dashboard" below.
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
- `Endpoint`: `id`, `url`, `head` (JSON), `body` (JSON), `type` (`token` / `event` / `notification`), `is_active` (only active notification endpoints are POSTed to). Seeded rows: `zk-token`, `zk-event`, `work-order` (dummy, active), `work-order-real` (Algospection, **inactive** until access is granted). `head`/`body` may contain `{{client_id}}`, `{{client_secret}}`, `{{algospection_token}}`, filled from `.env` at request time.
- `DeviceList`: `id` (ZK device ID; initial `2069691213314072577`), `type`, `current_count` (default 0), `maximum_trigger` (default 10, editable in Admin), `baseline_done` (False until the first sync stores the baseline), `count_date` (WIB day the current count belongs to).
- `SchedulerConfig`: singleton (`pk=1`) with `interval_seconds` (ZK sync, default 60, 10–86400), `enabled` and `dashboard_refresh_seconds`.
- `EventLog`: `id` (ZK event id), `metadata_id`, `time`, `track_id`, `event_type`, `recognition_target`, `height`, `device` FK, `counted` (whether it was added to `current_count`).
- `SensorLog`: log of ZK requests — `id` (UUID), `status` (`ONLINE`/`OFFLINE`), `response` (JSON), `time`, `endpoint_url`, `device` FK (null for token requests). The access token is never stored.
- `NotificationLog`: log of Work Order POSTs — `id` (UUID), `time`, `device` FK, `endpoint_url`, `body`, `head`, `response` (JSON), `response_status`.
- All FKs use `on_delete=CASCADE`.

**Work Order target (Algospection, `https://issid-inspection.com/api_iot.php`):** not accessible yet, so build a **dummy API** in a separate Django app (e.g. `dummy_wo`, `POST /dummy/api_iot.php`). It validates the static `token` and required fields, stores requests in its own table (visible in Admin), and replies like the Algospection doc (`{"status":"success","message":"Work Order berhasil dibuat","wo_id":...}`, or 401 `{"status":"error",...}`). The notification `Endpoint` points to the dummy for now. Body uses the doc example values but with the dummy token, `INSTANCE: "prod"`, `LOC_ID: "GRAHA ISS BINTARO"`, `ASSET_ID: "SPACE-GRAHAISS-0020"`, `REQ_TYP: "CHECK-ROOM-TOILET"`, `REQ_DESC: "Toilet Traffic Counter"`.

**Secrets:** ZK client ID/secret, `ALGOSPECTION_TOKEN`, `DUMMY_WO_TOKEN` and DB credentials go in `.env` (never committed).

## Washroom API (decisions confirmed with user, 2026-10-02)

The ZK side POSTs washroom sensor data and rating presses through this API (`washroom/` app); we define the payload format. The API follows the dashboard's data model exactly (decision 2026-10-02): no device/location models of its own, everything hangs off `core.DeviceList` (`type`, `building`, `floor`, `gender`). The only dashboard is the team's `dashboard/` app; its sensor cards are not wired to this data yet (still "Segera Hadir").

- Auth: `X-API-Key` header, one `ApiClient` per external system (Admin or `create_api_client`; raw key shown once, only SHA-256 hash stored). Admin sessions cannot call the API. `/api/docs/` (Swagger) and `/api/schema/` are public.
- `POST /api/v1/readings/` — JSON object or list (max 500, all or nothing): `device_id`, `type` (`soap` / `toilet-paper` / `tissue` / `trash` / `ammonia`, i.e. `DeviceList.type`), optional `time` (default now; naive = WIB), `battery` (0-100), `level` (%, or ppm for ammonia). Stored in `SensorReading` (FK to `DeviceList`) with the raw `payload`.
- `POST /api/v1/customer-responses/` — `device_id` (a `satisfaction` device), `rating` (1-5), optional `time`, `comment`. Stored in `CustomerResponse`.
- An unknown `device_id` is registered in `DeviceList` with that type and an empty location (set building/floor/gender in Admin so the dashboard shows it); a device registered with another type (including `people`) is rejected. `sync_all` only syncs `people`, so these devices never call ZK.
- `GET` on both endpoints lists history, filterable by `device_id`, `building`, `floor`, `gender`, `time_from`, `time_to` (readings also `type`); responses include the device's building/floor/gender.
- Condition is computed by the server: `StatusRule` per type matches `min_level <= level < max_level` (seeded in `washroom/0002`, editable in Admin) → `condition` + `severity` (`normal`/`warning`/`critical`).
- Errors are always `{"status": "error", "message": ..., "errors"?: [...]}` (`washroom/exceptions.py`).
- Base URL shown in Swagger comes from `API_BASE_URL` (.env; temporary default `http://192.168.10.120:8080`, the dev server). Integration guide for the ZK side: `docs/Dokumentasi_API_Washroom.pdf` (Indonesian; regenerate it when endpoints or the base URL change).

## Commands

Use the venv: `.venv\Scripts\python` (deps in `requirements.txt`). Config comes from `.env` (template: `.env.example`). PostgreSQL 16 runs locally on 5432, DB `people_counting`.

- Web + Admin + dummy Work Order API: `python manage.py runserver`
- Scheduler (separate terminal, keeps running): `python manage.py run_scheduler`
- One sync cycle manually: `python manage.py sync_once`
- Backfill history (no counting, no Work Order): `python manage.py backfill_events --date YYYY-MM-DD [--device ID]`
- Migrations: `python manage.py makemigrations` then `python manage.py migrate` (`core/0002` seeds endpoints, the device and scheduler config)
- All tests: `python manage.py test`; single test: `python manage.py test core.tests.SyncDeviceTests.test_counts_only_in_cross_line`
- Admin user: `python manage.py createsuperuser`
- Washroom API key: `python manage.py create_api_client <name> [--regenerate]` (prints the key once; Docker: `docker compose exec web python manage.py create_api_client <name>`)
- Washroom API end-to-end test: `python manage.py send_dummy_data [--scenario random|normal|warning|critical] [--building B --floor F --gender male|female] [--ratings N] [--base-url URL]` registers `DUMMY-*` devices (all reading types + satisfaction) on a toilet (default: the first located people counter's) and POSTs over HTTP with a regenerated `dummy-tester` key; `--cleanup` deletes those devices with their data. Docker: `docker compose exec web python manage.py send_dummy_data --base-url http://127.0.0.1:8000`

## Docker

- `docker-compose.yml` (production-style): `db` (postgres:16), `web` (Gunicorn; runs migrate + collectstatic on start via `docker/entrypoint.sh` when `RUN_MIGRATIONS=1`), `scheduler` (`run_scheduler`), `nginx` (port `NGINX_PORT`, serves `/static/`). Config from `.env`; compose overrides `DB_HOST=db`.
- Dev: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` (runserver on :8000, source mounted, Postgres on host :5433).
- The dummy Work Order URL inside Docker is `http://web.internal:8000/dummy/api_iot.php` (dotted network alias, because Admin's URLField rejects bare `web`). `web.internal` must be in `DJANGO_ALLOWED_HOSTS`.
- Only one `scheduler` container may run, otherwise events are processed twice.

## Dashboard (public, `dashboard/` app — decisions 2026-10-02)

Design: Claude Design canvas "Washroom Dashboard Screens" (https://claude.ai/artifact/QUWp7a1vDfAXLi1Y7Viu47) + design system "Washroom Dashboard" (https://claude.ai/artifact/W1mHduWJgrHXB8P7f5xHVu). Screens: Overview (7 section cards; only People Counting active, others "Segera Hadir"), dark + mobile variants, People Counting detail, Work Order drawer, states.

- Built as Django pages in this project (not React, not merged into washroom.inovasiadiwarna.com).
- Public, no login (Admin stays login-only).
- A toilet = building + floor + gender (fields on `DeviceList`); Lantai/Gender filters pick the toilet.
- `DeviceList.type` = the module a device belongs to (`people`, `satisfaction`, `soap`, `toilet-paper`, `tissue`, `trash`, `ammonia`); a toilet can have several devices per module. Each module page has a "Device" dropdown (default "Semua device" = summed; per-device counters/thresholds listed separately). `DeviceList.name` is the dropdown label (falls back to id).
- Only `type="people"` devices are synced from ZK (`sync_all`, `backfill_events`).
- People Counting: KPIs (IN today, current_count/maximum_trigger labelled "saat ini" even for past dates, WO sent today success/failed, sensor status), hourly IN chart with WO markers, Rekap Harian (+CSV/Excel), Riwayat Work Order (wo_id from `response.wo_id`, "–" if failed), drawer with request/response JSON. No device/sensor log table on the dashboard (removed at user request; SensorLog stays in Admin).
- No sensor ONLINE/OFFLINE status on the dashboard (removed at user request); SensorLog stays in Admin.
- Mask the Algospection/dummy `token` in the drawer (e.g. `iss_b2f•••••`).
- Auto-refresh: `dashboard.js` re-fetches the page every `SchedulerConfig.dashboard_refresh_seconds` (Admin, 10–3600 s) and swaps `#content` + `[data-alerts]`; shows "Diperbarui HH:MM:SS", an offline banner with backoff, refreshes on tab focus. A server-side stale banner appears when the last ONLINE SensorLog is older than max(5 min, 3 × sync interval) (today only).
- Language: ID (default) / EN via `?lang=` → cookie `wd_lang`. All UI text lives in `dashboard/i18n.py` (dict, no gettext — `msgfmt` is not installed); templates use `{{ t.key }}` or `{% tr "key" n=… %}` (`dashboard/templatetags/dashboard_tags.py`). Views are wrapped in `@localized` so dates/numbers (`|intcomma`) follow the language. Module names come from i18n `module_<type>`.
- Fonts are local (`static/dashboard/fonts`, OFL); custom `templates/404.html` / `500.html`.

## Code Layout

- `core/services.py` — the whole sync engine (`ZKClient`, `fetch_new_events`, `sync_device`, `dispatch_work_orders`, `backfill_events`, `sync_all`). `fetch_new_events` stops after `MAX_PAGES` (50) pages per cycle.
- `core/management/commands/run_scheduler.py` — APScheduler loop; re-reads `SchedulerConfig` every 30s to apply interval changes.
- Exports (export only, CSV/XLSX): `EventLog`, `SensorLog`, `NotificationLog` via django-import-export (`core/resources.py`, follows active Admin filters); Daily recap via its own `?export=csv|xlsx` on the recap page (`build_recap_rows` in `core/admin.py`, with `date_from`/`date_to` filter; exports include every day).
- `core/admin.py` — log models are read-only; `EventLog` has a custom `TimeRangeFilter` (from/to datetime); `DailyRecap` (proxy of `EventLog`, migration `0004`) renders a per-day recap page (events, counted, Work Orders sent/success/failed) from `core/templates/admin/core/dailyrecap/change_list.html`.
- `dashboard/` — public dashboard: `/` overview, `/people-counting/` detail, `/people-counting/rekap.<csv|xlsx>`. `queries.py` holds all reads (token masking in `mask_secrets`); `static/dashboard/` has the CSS (light/dark tokens), JS (drawer, theme, 1-min refresh) and logos (`img/isslogo.jpg`, `img/wirapandulogo.jpg`; text fallback when missing).
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
