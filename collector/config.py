"""Configuration, read once from the environment.

Everything is validated at startup so a misconfiguration fails immediately and
loudly, rather than at 03:00 on the first cold night.
"""
import os
from datetime import datetime


class ConfigError(RuntimeError):
    pass


def _require(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            "%s is not set. Copy .env.example to .env and fill it in." % name
        )
    return value


def _int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("%s must be an integer, got %r" % (name, raw))


class Config:
    def __init__(self):
        self.hub_ip = _require("WISER_HUB_IP")
        self.hub_secret = _require("WISER_SECRET")
        self.owm_key = _require("OWM_API_KEY")
        self.s3_bucket = _require("S3_BUCKET")

        self.db_path = os.environ.get("DB_PATH", "/data/wiser.db")
        self.publish_dir = os.environ.get("PUBLISH_DIR", "/data/published")

        # Cadences. The hub is polled 3x more often than the weather because
        # outside temperature moves slowly - see the requirements doc.
        self.poll_seconds = _int("POLL_SECONDS", 300)
        self.weather_every = _int("WEATHER_EVERY_N_POLLS", 3)
        self.s3_every = _int("S3_EVERY_N_POLLS", 12)
        self.db_backup_every = _int("DB_BACKUP_EVERY_N_POLLS", 288)

        # Open-Meteo hourly back-fill (no key needed). On by default.
        self.openmeteo_enabled = os.environ.get("OPENMETEO_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
        self.openmeteo_every_hours = _int("OPENMETEO_EVERY_HOURS", 6)
        self.openmeteo_recent_days = _int("OPENMETEO_RECENT_DAYS", 7)
        # First day to back-fill. Empty = the day logging began (first hub sample).
        self.openmeteo_start = os.environ.get("OPENMETEO_START", "").strip() or None
        if self.openmeteo_start:
            try:
                datetime.strptime(self.openmeteo_start, "%Y-%m-%d")
            except ValueError:
                raise ConfigError("OPENMETEO_START must be YYYY-MM-DD, got %r" % self.openmeteo_start)
        if not 1 <= self.openmeteo_recent_days <= 92:
            raise ConfigError("OPENMETEO_RECENT_DAYS must be between 1 and 92")

        # Smart-meter energy via Hildebrand Glow's Bright service. Optional:
        # off unless both the Bright login settings are present.
        self.glow_username = os.environ.get("GLOW_USERNAME", "").strip() or None
        self.glow_password = os.environ.get("GLOW_PASSWORD", "").strip() or None
        if bool(self.glow_username) != bool(self.glow_password):
            raise ConfigError("Set both GLOW_USERNAME and GLOW_PASSWORD (your Bright app login), or neither")
        self.glow_enabled = bool(self.glow_username)
        self.glow_every_hours = _int("GLOW_EVERY_HOURS", 6)
        self.glow_history_days = _int("GLOW_HISTORY_DAYS", 395)    # ~13 months, what Bright offers
        self.glow_classifiers = [c.strip() for c in os.environ.get(
            "GLOW_CLASSIFIERS", "gas.consumption,electricity.consumption").split(",") if c.strip()]
        if self.glow_enabled and not self.glow_classifiers:
            raise ConfigError("GLOW_CLASSIFIERS is empty")

        self.metrics_port = _int("METRICS_PORT", 9109)
        self.http_timeout = _int("HTTP_TIMEOUT", 20)

    def __repr__(self):
        # Deliberately lists no secret values - see the wiser.log incident.
        return (
            "Config(hub=%s, bucket=%s, poll=%ss, weather_every=%s, s3_every=%s,"
            " openmeteo=%s every %sh, glow=%s)"
            % (self.hub_ip, self.s3_bucket, self.poll_seconds,
               self.weather_every, self.s3_every,
               "on" if self.openmeteo_enabled else "off", self.openmeteo_every_hours,
               "on" if self.glow_enabled else "off")
        )
