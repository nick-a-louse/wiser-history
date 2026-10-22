"""Collector entrypoint: poll, store, publish, and stay observable.

The original project failed every 15 minutes for years without anyone noticing,
because cron discarded stderr. Three things here exist specifically so that
cannot recur:

  * every cycle writes a run_log row, successful or not
  * /metrics exposes last-success age for Grafana to alert on
  * failures log loudly to stdout, which Docker captures
"""
import logging
import os
import signal
import sys
import threading
import time
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import Config, ConfigError
from .glow import Glow, GlowError, latest_readings as glow_latest, sync as glow_sync
from .hub import Hub, HubError, geo_position, parse
from .openmeteo import OpenMeteo, OpenMeteoError, sync as openmeteo_sync
from .publish import S3Publisher, backup_db, generate
from .store import Store, iso, utcnow
from .weather import Weather, WeatherError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
# botocore at DEBUG writes SigV4 material - including the access key id - into
# the log. That is exactly how the old project leaked a credential.
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

log = logging.getLogger("wiser")

# Release number; the image build adds its git commit (APP_VERSION, e.g. "sha-7eec055...").
RELEASE = "1.3.0"
_build = os.environ.get("APP_VERSION", "").strip()
VERSION = RELEASE if not _build or _build.lstrip("v") == RELEASE else "%s (%s)" % (RELEASE, _build[:11])

STATE = {
    "last_success_epoch": 0.0,
    "last_hub_ok": 0,
    "last_weather_ok": 0,
    "last_publish_ok": 0,
    "last_openmeteo_ok": 0,
    "openmeteo_last_success_epoch": 0.0,
    "openmeteo_next_epoch": 0.0,      # 0 = due now (first cycle after start-up)
    "last_glow_ok": 0,
    "glow_last_success_epoch": 0.0,
    "glow_next_epoch": 0.0,
    "glow_latest": {},                # {classifier: unix time of newest stored half-hour}
    "cycles": 0,
    "failures": 0,
}


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return

        age = time.time() - STATE["last_success_epoch"] if STATE["last_success_epoch"] else -1

        if self.path == "/healthz":
            healthy = 0 <= age < 1800
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n" if healthy else b"stale\n")
            return

        body = (
            "# HELP wiser_build_info Build information.\n"
            "# TYPE wiser_build_info gauge\n"
            'wiser_build_info{version="%s"} 1\n' % VERSION
        ) + (
            # A timestamp, not just an age: `time() - timestamp` keeps climbing
            # correctly across a scrape gap, whereas a stale age value would
            # keep reporting whatever it was when the collector last answered.
            "# HELP wiser_last_success_timestamp_seconds Unix time of the last successful hub poll.\n"
            "# TYPE wiser_last_success_timestamp_seconds gauge\n"
            "wiser_last_success_timestamp_seconds %.0f\n"
            "# HELP wiser_last_success_age_seconds Seconds since the last successful hub poll.\n"
            "# TYPE wiser_last_success_age_seconds gauge\n"
            "wiser_last_success_age_seconds %.1f\n"
            "# HELP wiser_hub_up Whether the most recent hub poll succeeded.\n"
            "# TYPE wiser_hub_up gauge\n"
            "wiser_hub_up %d\n"
            "# HELP wiser_weather_up Whether the most recent weather poll succeeded.\n"
            "# TYPE wiser_weather_up gauge\n"
            "wiser_weather_up %d\n"
            "# HELP wiser_publish_up Whether the most recent S3 publish succeeded.\n"
            "# TYPE wiser_publish_up gauge\n"
            "wiser_publish_up %d\n"
            "# HELP wiser_openmeteo_up Whether the most recent Open-Meteo sync succeeded.\n"
            "# TYPE wiser_openmeteo_up gauge\n"
            "wiser_openmeteo_up %d\n"
            "# HELP wiser_openmeteo_last_success_timestamp_seconds Unix time of the last successful Open-Meteo sync.\n"
            "# TYPE wiser_openmeteo_last_success_timestamp_seconds gauge\n"
            "wiser_openmeteo_last_success_timestamp_seconds %.0f\n"
            "# HELP wiser_cycles_total Completed collection cycles.\n"
            "# TYPE wiser_cycles_total counter\n"
            "wiser_cycles_total %d\n"
            "# HELP wiser_failures_total Cycles that raised an error.\n"
            "# TYPE wiser_failures_total counter\n"
            "wiser_failures_total %d\n"
        ) % (STATE["last_success_epoch"], age, STATE["last_hub_ok"],
             STATE["last_weather_ok"], STATE["last_publish_ok"],
             STATE["last_openmeteo_ok"], STATE["openmeteo_last_success_epoch"],
             STATE["cycles"], STATE["failures"])

        if STATE["glow_latest"] or STATE["last_glow_ok"] or STATE["glow_last_success_epoch"]:
            body += (
                "# HELP wiser_glow_up Whether the most recent smart-meter (Bright) sync succeeded.\n"
                "# TYPE wiser_glow_up gauge\n"
                "wiser_glow_up %d\n"
                "# HELP wiser_glow_last_success_timestamp_seconds Unix time of the last successful smart-meter sync.\n"
                "# TYPE wiser_glow_last_success_timestamp_seconds gauge\n"
                "wiser_glow_last_success_timestamp_seconds %.0f\n"
                "# HELP wiser_glow_latest_reading_timestamp_seconds Unix time of the newest stored half-hour, per meter (0 = none yet).\n"
                "# TYPE wiser_glow_latest_reading_timestamp_seconds gauge\n"
            ) % (STATE["last_glow_ok"], STATE["glow_last_success_epoch"])
            for classifier, ts in sorted(STATE["glow_latest"].items()):
                body += 'wiser_glow_latest_reading_timestamp_seconds{classifier="%s"} %.0f\n' % (classifier, ts)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass  # don't spam stdout with scrape requests


def serve_metrics(port):
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("metrics listening on :%d/metrics", port)


# After a failed Open-Meteo sync, try again after this long - not every cycle,
# which would hit a struggling free service 12 times an hour.
OPENMETEO_RETRY_SECONDS = 3600


def openmeteo_due(cfg):
    return cfg.openmeteo_enabled and time.time() >= STATE["openmeteo_next_epoch"]


def run_openmeteo(cfg, store, client, raw, now):
    """Sync weather_hourly. Returns (ok, error). Never raises, and never touches
    the hub data: an Open-Meteo outage must not cost a single 5-minute reading."""
    try:
        lat, lon = geo_position(raw)
        start = cfg.openmeteo_start or (store.coverage().get("first") or iso(now))[:10]
        openmeteo_sync(store, client, lat, lon, start, now, cfg.openmeteo_recent_days)
        STATE["openmeteo_last_success_epoch"] = time.time()
        STATE["openmeteo_next_epoch"] = time.time() + cfg.openmeteo_every_hours * 3600
        return True, None
    except (OpenMeteoError, HubError) as exc:
        error = str(exc)
    except Exception as exc:              # a bug here must not stop the hub loop
        log.exception("unexpected error in open-meteo sync")
        error = "open-meteo: %s" % type(exc).__name__
    log.error("open-meteo sync failed: %s", error)
    STATE["openmeteo_next_epoch"] = time.time() + OPENMETEO_RETRY_SECONDS
    return False, error


# Same back-off as Open-Meteo after a failed smart-meter sync.
GLOW_RETRY_SECONDS = 3600


def glow_due(cfg):
    return cfg.glow_enabled and time.time() >= STATE["glow_next_epoch"]


def run_glow(cfg, store, client, now):
    """Sync energy_halfhourly from Bright. Returns (ok, error). Never raises."""
    try:
        glow_sync(store, client, cfg.glow_classifiers, cfg.glow_history_days, now)
        STATE["glow_last_success_epoch"] = time.time()
        STATE["glow_next_epoch"] = time.time() + cfg.glow_every_hours * 3600
        ok, error = True, None
    except GlowError as exc:
        ok, error = False, str(exc)
    except Exception as exc:              # a bug here must not stop the hub loop
        log.exception("unexpected error in glow sync")
        ok, error = False, "glow: %s" % type(exc).__name__
    if not ok:
        log.error("glow sync failed: %s", error)
        STATE["glow_next_epoch"] = time.time() + GLOW_RETRY_SECONDS
    try:
        STATE["glow_latest"] = glow_latest(store, cfg.glow_classifiers)
    except Exception:
        log.exception("couldn't read the newest smart-meter readings")
    return ok, error


def cycle(cfg, store, hub, weather, publisher, count, openmeteo=None, glow=None):
    """One collection cycle. Never raises: a failure is data, not a crash."""
    started = time.time()
    now = utcnow()
    ts = iso(now)
    hub_ok = weather_ok = publish_ok = openmeteo_ok = glow_ok = None
    error = None

    try:
        raw = hub.fetch()
        system, rooms, devices = parse(raw)
        store.record_rooms(ts, rooms)
        store.record_sample(ts, system, rooms, devices)
        hub_ok = True
        STATE["last_success_epoch"] = time.time()
        log.info("hub ok: %d rooms, %d devices, boiler=%s",
                 len(rooms), len(devices), system.get("boiler_relay"))
    except HubError as exc:
        hub_ok = False
        error = str(exc)
        log.error("hub poll failed: %s", exc)
        raw = None

    # Weather is polled less often than the hub - it moves slowly.
    if raw is not None and count % cfg.weather_every == 0:
        try:
            lat, lon = geo_position(raw)
            store.record_weather(ts, weather.current(lat, lon))
            weather_ok = True
            log.info("weather ok")
        except (WeatherError, HubError) as exc:
            weather_ok = False
            error = "; ".join(filter(None, [error, str(exc)]))
            log.error("weather poll failed: %s", exc)

    # Hourly back-fill, every few hours. After the hub and OWM, so it can only
    # ever delay their next cycle, never this one's readings.
    if raw is not None and openmeteo is not None and openmeteo_due(cfg):
        openmeteo_ok, om_error = run_openmeteo(cfg, store, openmeteo, raw, now)
        if om_error:
            error = "; ".join(filter(None, [error, om_error]))

    # Smart-meter energy, every few hours. Doesn't need the hub (the meter
    # data comes from Bright), so it runs even if the hub poll failed.
    if glow is not None and glow_due(cfg):
        glow_ok, glow_error = run_glow(cfg, store, glow, now)
        if glow_error:
            error = "; ".join(filter(None, [error, glow_error]))

    if hub_ok:
        try:
            written = generate(store, cfg.publish_dir, cfg.poll_seconds,
                               cfg.poll_seconds * cfg.weather_every)
            if publisher and count % cfg.s3_every == 0:
                if count % cfg.db_backup_every == 0:
                    store.commit()
                    written = written + [backup_db(store.path, cfg.publish_dir)]
                publisher.upload(cfg.publish_dir, written)
                publish_ok = True
        except Exception as exc:
            publish_ok = False
            error = "; ".join(filter(None, [error, "publish: %s" % exc]))
            log.error("publish failed: %s", exc)

    duration_ms = int((time.time() - started) * 1000)
    store.record_run(ts, bool(hub_ok), weather_ok, publish_ok, duration_ms, error,
                     openmeteo_ok, glow_ok)
    store.commit()

    STATE["last_hub_ok"] = int(bool(hub_ok))
    if weather_ok is not None:
        STATE["last_weather_ok"] = int(weather_ok)
    if publish_ok is not None:
        STATE["last_publish_ok"] = int(publish_ok)
    if openmeteo_ok is not None:
        STATE["last_openmeteo_ok"] = int(openmeteo_ok)
    if glow_ok is not None:
        STATE["last_glow_ok"] = int(glow_ok)
    STATE["cycles"] += 1
    if error:
        STATE["failures"] += 1


def main():
    try:
        cfg = Config()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info("starting: %s", cfg)
    os.makedirs(os.path.dirname(cfg.db_path), exist_ok=True)

    store = Store(cfg.db_path)
    hub = Hub(cfg.hub_ip, cfg.hub_secret, cfg.http_timeout)
    weather = Weather(cfg.owm_key, cfg.http_timeout)
    openmeteo = OpenMeteo(max(cfg.http_timeout, 30)) if cfg.openmeteo_enabled else None
    glow = Glow(cfg.glow_username, cfg.glow_password, max(cfg.http_timeout, 30)) if cfg.glow_enabled else None

    try:
        publisher = S3Publisher(cfg.s3_bucket)
    except Exception as exc:
        log.error("S3 unavailable, continuing without publishing: %s", exc)
        publisher = None

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    serve_metrics(cfg.metrics_port)

    count = 0
    while not stopping.is_set():
        try:
            cycle(cfg, store, hub, weather, publisher, count, openmeteo, glow)
        except Exception:
            STATE["failures"] += 1
            log.exception("unexpected error in collection cycle")
        count += 1

        # Align to the wall clock so samples land on tidy boundaries and a slow
        # cycle doesn't drift the schedule.
        delay = cfg.poll_seconds - (time.time() % cfg.poll_seconds)
        stopping.wait(delay)

    log.info("shutting down")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
