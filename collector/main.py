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
from .hub import Hub, HubError, geo_position, parse
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

STATE = {
    "last_success_epoch": 0.0,
    "last_hub_ok": 0,
    "last_weather_ok": 0,
    "last_publish_ok": 0,
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
            "# HELP wiser_cycles_total Completed collection cycles.\n"
            "# TYPE wiser_cycles_total counter\n"
            "wiser_cycles_total %d\n"
            "# HELP wiser_failures_total Cycles that raised an error.\n"
            "# TYPE wiser_failures_total counter\n"
            "wiser_failures_total %d\n"
        ) % (STATE["last_success_epoch"], age, STATE["last_hub_ok"],
             STATE["last_weather_ok"], STATE["last_publish_ok"],
             STATE["cycles"], STATE["failures"])

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


def cycle(cfg, store, hub, weather, publisher, count):
    """One collection cycle. Never raises: a failure is data, not a crash."""
    started = time.time()
    now = utcnow()
    ts = iso(now)
    hub_ok = weather_ok = publish_ok = None
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
    store.record_run(ts, bool(hub_ok), weather_ok, publish_ok, duration_ms, error)
    store.commit()

    STATE["last_hub_ok"] = int(bool(hub_ok))
    if weather_ok is not None:
        STATE["last_weather_ok"] = int(weather_ok)
    if publish_ok is not None:
        STATE["last_publish_ok"] = int(publish_ok)
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
            cycle(cfg, store, hub, weather, publisher, count)
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
