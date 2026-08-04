"""Configuration, read once from the environment.

Everything is validated at startup so a misconfiguration fails immediately and
loudly, rather than at 03:00 on the first cold night.
"""
import os


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

        self.metrics_port = _int("METRICS_PORT", 9109)
        self.http_timeout = _int("HTTP_TIMEOUT", 20)

    def __repr__(self):
        # Deliberately lists no secret values - see the wiser.log incident.
        return (
            "Config(hub=%s, bucket=%s, poll=%ss, weather_every=%s, s3_every=%s)"
            % (self.hub_ip, self.s3_bucket, self.poll_seconds,
               self.weather_every, self.s3_every)
        )
