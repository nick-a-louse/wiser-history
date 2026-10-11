# Wiser heating history

Records Drayton Wiser heating data plus local weather every 5 minutes, and serves
an interactive site for exploring years of it.

Two components, deliberately decoupled — they share only the published data files,
so either can be rewritten without touching the other:

| | What it does |
|---|---|
| **`collector/`** | Polls the Wiser hub and OpenWeatherMap, stores everything in SQLite, generates the static data files, mirrors them to a private S3 bucket |
| **`web/`** | nginx serving a single-page ECharts site that reads those files — zoomable per-room temperature and humidity against outside conditions |

Full design rationale lives in Obsidian: *Atlas/Technology/Server/Wiser Requirements*.

## Why this exists

It replaces `/opt/wiser_logging`, which failed on **every run for years** without
anyone noticing, because its cron entry ended `>/dev/null 2>&1`. Several choices
here are direct responses to that:

| Decision | Reason |
|---|---|
| No third-party Wiser library | The predecessor died when `wiserHeatingAPI` was abandoned. The hub is one JSON GET with one header |
| Containers with internal schedulers | No root cron, no discarded stderr, no venv to break on the next distro upgrade |
| `run_log` row every cycle + `/metrics` | Silent death becomes visible. A crash-only alert would not have caught the original |
| Rooms keyed on hub id, never name | A rename would otherwise fragment the history silently |
| UTC everywhere | The hub reports `TimeZoneOffset: 0` while sitting in BST |
| Weather responses scrubbed | OWM embeds the API key in the `prev`/`next` URLs it returns |
| `botocore` pinned below DEBUG | That logger put an AWS key id into the old world-readable `wiser.log` |

The room-id decision has since proved itself: two rooms were renamed in August
2026 and both kept a single unbroken series across the change.

## Layout

```
collector/config.py    environment parsing, validated at startup
collector/hub.py       Wiser hub client + response parsing
collector/weather.py   OpenWeatherMap One Call 4.0 client (scrubs the API key)
collector/store.py     SQLite schema, writes, and the rollup queries
collector/publish.py   static file generation + S3 upload
collector/main.py      scheduler, metrics endpoint, cycle orchestration

web/index.html         the page
web/app.js             chart construction, tier switching, gap handling
web/styles.css         palette tokens, light + dark
web/nginx.conf         static serving, cache policy, MIME types
web/Dockerfile         vendors ECharts at build time (no CDN at runtime)
```

## Running

```bash
install -m 600 /dev/null .env    # create locked down, THEN fill in
docker compose up -d --build
docker compose logs -f
```

Two containers come up: `wiser-collector` (metrics on **:9109**) and `wiser-web`
(the site on **:5059**).

State lives in `/opt/appdata/wiser`:

```
wiser.db              source of truth
published/data/       the JSON the website reads
published/backup/     gzipped DB, mirrored to S3
```

The web container mounts `published/` **read-only** — the site renders the data
and can never alter it. S3 holds a private off-site copy; the IAM user has
`PutObject` and nothing else, so a misbehaving collector cannot read or destroy
its own backup.

## The website

Range presets (24h / 7d / 30d / 1y / All) with drag-zoom, scroll-zoom and pinch;
per-room legend toggling; optional target-temperature and boiler-firing overlays;
light and dark; a data table; and a freshness indicator that turns red when
collection goes stale.

Temperature and humidity are **two linked charts, not one chart with two y-axes** —
different scales in one plot invite false correlations. They share an x-axis and a
single zoom control.

Colour uses eight categorical hues validated for colour-vision deficiency in both
modes. With ten rooms, the last two reuse the first two hues with a dashed stroke
rather than inventing a ninth hue that would fail the check. Hue is bound to room
id, so a room keeps its colour whichever others are hidden.

## Data tiers

The site picks a tier from the zoom range, so a ten-year view fetches one small
file rather than a million points:

| Range | File | Resolution |
|---|---|---|
| < 7 days | `data/raw/YYYY-MM.json` | native 5-minute |
| 7–90 days | `data/30min/YYYY.json` | half-hourly means |
| > 90 days | `data/daily/all.json` | daily min/max/mean |

`data/meta.json` carries the room list, rename history, coverage, known gaps,
last success, and both cadences.

> The hub is polled every 5 minutes but weather only every 15. Gap detection must
> use each series' **own** interval — checking weather against the hub's cadence
> marks every weather point as a gap and the outside line disappears entirely.
> That is what `weather_seconds` in `meta.json` is for.

## Monitoring

`http://<host>:9109/metrics` exposes Prometheus metrics, scraped by
VictoriaMetrics. The one that matters is `wiser_last_success_timestamp_seconds` —
alert when `time() - ` it exceeds ~1800. Grafana also alerts on the weather API
and the S3 publish failing.

`/healthz` returns 503 once the last success is over 30 minutes old, which the
container healthcheck uses.

## Gaps

Missing readings are **never interpolated**. `meta.json` lists detected gaps and
the site draws them as breaks in the line, so an outage looks like an outage.
Given this project's history of failures hiding in plain sight, the chart should
make them more visible, not less.
