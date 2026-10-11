"""Generate the static data files, and mirror them to S3.

The site reads these files from local disk. S3 is a private off-site copy, not a
serving path - which is why the IAM user has PutObject and nothing else.

Three resolution tiers exist so that a ten-year view fetches one small file
rather than a million points:

    < 7 days    raw/YYYY-MM.json      native 5-minute
    7-90 days   30min/YYYY.json       half-hourly means
    > 90 days   daily/all.json        daily min/max/mean
"""
import gzip
import json
import logging
import os
import shutil
import tempfile

log = logging.getLogger("wiser.publish")


def _write_json(path, payload):
    """Write atomically, so a crash mid-write can't leave a truncated file
    that the website would happily render as missing data."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"), default=str)
        # mkstemp creates 0600. The web container runs as a different uid and
        # only ever reads these, so they must be world-readable.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def generate(store, out_dir, poll_seconds, weather_seconds=None):
    """Regenerate the published file set. Returns the list of relative paths."""
    written = []

    months = store.months()
    for month in months[-2:]:  # only the current and previous month change
        rel = "data/raw/%s.json" % month
        _write_json(os.path.join(out_dir, rel), store.raw_month(month))
        written.append(rel)

    years = sorted({m[:4] for m in months})
    for year in years[-1:]:
        rel = "data/30min/%s.json" % year
        _write_json(os.path.join(out_dir, rel), store.rollup_30min(year))
        written.append(rel)

    rel = "data/daily/all.json"
    _write_json(os.path.join(out_dir, rel), store.rollup_daily())
    written.append(rel)

    coverage = store.coverage()
    meta = {
        "rooms": store.rooms(),
        "renames": store.renames(),
        "coverage": coverage,
        "gaps": store.gaps(poll_seconds),
        "last_success_utc": store.last_success(),
        "poll_seconds": poll_seconds,
        # Weather is polled less often than the hub. The site needs this
        # separately: using the hub's interval to detect gaps in the weather
        # series marks every single point as a gap and the line vanishes.
        "weather_seconds": weather_seconds or poll_seconds,
        "months": months,
        "years": years,
        "tiers": {"raw_days": 7, "half_hourly_days": 90},
    }
    rel = "data/meta.json"
    _write_json(os.path.join(out_dir, rel), meta)
    written.append(rel)

    return written


def backup_db(db_path, out_dir):
    """Gzip the SQLite file so S3 backs up the source of truth, not just the
    derived views."""
    rel = "backup/wiser.db.gz"
    dest = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with open(db_path, "rb") as src, gzip.open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, dest)
    return rel


class S3Publisher:
    def __init__(self, bucket):
        import boto3  # imported lazily so the collector runs without AWS in tests
        self.bucket = bucket
        self.client = boto3.client("s3")

    def upload(self, out_dir, relative_paths):
        sent = 0
        for rel in relative_paths:
            path = os.path.join(out_dir, rel)
            if not os.path.exists(path):
                continue
            content_type = "application/gzip" if rel.endswith(".gz") else "application/json"
            with open(path, "rb") as handle:
                self.client.put_object(
                    Bucket=self.bucket, Key=rel, Body=handle, ContentType=content_type
                )
            sent += 1
        log.info("uploaded %d file(s) to s3://%s", sent, self.bucket)
        return sent
