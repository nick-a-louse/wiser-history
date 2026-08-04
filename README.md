# Wiser collector

Logs Drayton Wiser heating data plus local weather every 5 minutes, into SQLite,
and mirrors it to a private S3 bucket. Feeds an interactive site (separate
component, not yet built).

Full design rationale lives in Obsidian: *Atlas/Technology/Server/Wiser/Wiser Requirements*.

## Why this exists

It replaces `/opt/wiser_logging`, which failed on every run for years without
anyone noticing, because its cron entry ended `>/dev/null 2>&1`. Several choices
here are direct responses to that:

| Decision | Reason |
|---|---|
| No third-party Wiser library | The predecessor died when `wiserHeatingAPI` was abandoned. The hub is one JSON GET with one header |
| Container with an internal scheduler | No root cron, no discarded stderr, no venv to break on the next distro upgrade |
| `run_log` row every cycle + `/metrics` | Silent death becomes visible. A crash-only alert would not have caught the original |
| Rooms keyed on hub id, never name | A rename would otherwise fragment the history silently |
| UTC everywhere | The hub reports `TimeZoneOffset: 0` while sitting in BST |
| Weather responses scrubbed | OWM embeds the API key in the `prev`/`next` URLs it returns |
| `botocore` pinned below DEBUG | That logger put an AWS key id into the old world-readable `wiser.log` |

## Layout

```
collector/config.py    environment parsing, validated at startup
collector/hub.py       Wiser hub client + response parsing
collector/weather.py   OpenWeatherMap One Call 4.0 client (scrubs the API key)
collector/store.py     SQLite schema, writes, and the rollup queries
collector/publish.py   static file generation + S3 upload
collector/main.py      scheduler, metrics endpoint, cycle orchestration
```

## Running

```bash
install -m 600 /dev/null .env    # create locked down, THEN fill in
docker compose up -d --build
docker compose logs -f
```

State lives in `/opt/appdata/wiser`:

```
wiser.db              source of truth
published/data/       the JSON the website reads
published/backup/     gzipped DB, mirrored to S3
```

## Data tiers

The site picks a tier from the zoom range, so a ten-year view fetches one small
file rather than a million points:

| Range | File | Resolution |
|---|---|---|
| < 7 days | `data/raw/YYYY-MM.json` | native 5-minute |
| 7-90 days | `data/30min/YYYY.json` | half-hourly means |
| > 90 days | `data/daily/all.json` | daily min/max/mean |

`data/meta.json` carries the room list, coverage, known gaps and last success.

## Monitoring

`http://gomez:9109/metrics` exposes Prometheus metrics; `wiser_last_success_age_seconds`
is the one that matters. Alert if it exceeds ~1800.

`/healthz` returns 503 once the last success is over 30 minutes old, which the
container healthcheck uses.

## Gaps

Missing readings are never interpolated. `meta.json` lists detected gaps and the
site draws them as breaks in the line, so an outage looks like an outage.
