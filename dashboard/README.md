# Dashboard

A small FastAPI service next to the controller worker, plus a single-page frontend it serves.
It polls Temporal, keeps a SQLite copy of what it learns, and serves JSON and the page. The
page reads the copy, never Temporal directly, so it loads fast and keeps working (marked as
last known) when the controller or Temporal is down.

## Run it

From the repo root (it imports `common.py`):

    uv add fastapi "uvicorn[standard]"
    uv run uvicorn dashboard.app:app --port 8000      # then open http://localhost:8000

Binds to loopback. There's no authentication yet and the refresh and update-check buttons act
on your hosts, so keep it on a network you trust or behind something that authenticates.

Settings (environment variables): `TEMPORAL_ADDRESS` (localhost:7233), `TEMPORAL_NAMESPACE`
(default), `DASHBOARD_DB` (dashboard.sqlite3), `POLL_INTERVAL_S` (15), `QUERY_TIMEOUT_S` (5),
`CONTROLLER_POLLER_MAX_AGE_S` (60). The service starts even if Temporal is down.

## Tests

    uv add --dev pytest
    uv run pytest dashboard                                  # backend logic, no Temporal needed
    node --test dashboard/frontend_tests/*.test.mjs          # frontend, no browser or npm install

The frontend tests use `frontend_tests/fixtures.json`, which is generated from the real
`Fleet.view()` so the JavaScript is tested against the payloads the API actually serves.
Regenerate it after changing the payload shape:

    uv run python -m dashboard.make_fixtures

## What the page shows

- **Fleet:** counts (each says how many hosts it covers), filter chips, one card per host.
- **Host:** metrics, OS updates with how long each has been waiting, hardware, containers with
  update status. More than 10 pending packages collapse behind a toggle.
- **Control plane banner:** controller down, Temporal unreachable, starting, dashboard error,
  and the dashboard service itself being unreachable are separate states. In all of them, host
  status reads "Last known" with a time instead of claiming online or offline.
- Missing data is never drawn as zero or healthy: "not checked yet", "not tracked", "unknown"
  and "check failed" each have their own wording (see `normalize.py`).

## Endpoints

- `GET /` the page. `GET /static/*` its files.
- `GET /api/hosts`: control plane state, fleet totals, every host normalized, each with
  `meta.stale` and `meta.stale_reason`.
- `GET /api/hosts/{host}`: one host plus `pending_updates` with `age_days`.
- `GET /api/health`: control plane only.
- `POST /api/poll`: poll Temporal now. The page uses it for Retry and to pick up fresh data
  after a signal.
- `POST /api/hosts/{host}/refresh` and `/check-updates`: send the signal. The response says
  whether it was `queued` (control plane degraded) or went to a running controller.

## Control plane states

`ok`, `controller_down`, `temporal_unreachable`, `starting`, `dashboard_error`. A host being
offline is never one of these: that's a successful query returning `offline`.

## Layout

| File | Job | Tested |
|---|---|---|
| `normalize.py` | snapshot to explicit UI states, fleet totals | pytest |
| `store.py`, `schema.sql` | SQLite copy, update aging rules | pytest |
| `service.py` | health classification, fan-out, API payloads | pytest, fake gateway |
| `static/render.js` | payload to HTML (pure, escapes everything) | node |
| `static/app.js` | fetching, routing, button actions | node, fake DOM |
| `gateway.py` | the only code that calls Temporal | no |
| `app.py` | HTTP routes | no |

## Behavior to know about

- After you click Refresh, the page waits about 4 seconds, asks the API to poll, and reloads.
  A host that's offline won't change, and a slow collection can miss the window; the next
  regular poll (every 10 seconds on the page) catches up. A proper fix is a Workflow Update
  that returns the fresh snapshot.
- Check for updates can run for minutes. The page says it started; results appear on later polls.

## Not done yet

- Authentication.
- Approving updates from the page (the Update handler and an allowlisted activity).
- Removing a retired host (its row and workflow stay until you terminate it).
