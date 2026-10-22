"""Open-Meteo hourly weather: back-fill and top-up for the weather_hourly table.

Why a second weather source:

* OWM gives one snapshot every 15 minutes and can't be back-filled. Anything
  not captured at the time is lost.
* Open-Meteo has hourly history for any location, measured sunlight
  (shortwave, direct and diffuse radiation in W/m2) and rain and snowfall
  amounts. The per-room heat model needs exactly those.

Two endpoints, both free for non-commercial use with no API key:

* archive  - reanalysis (ERA5 and friends). Final, but a few days behind.
* forecast - the forecast API's recent past (`past_days`), used for the last
             few days until the archive catches up, then replaced by it.

Like the hub and OWM clients, this uses only the standard library. Nothing
here logs a URL: it carries the home's coordinates.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wiser.openmeteo")

# One archive request covers at most this many days, so a long back-fill is a
# few modest requests rather than one huge one.
MAX_ARCHIVE_DAYS = 92

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo variable -> weather_hourly column.
VARIABLES = {
    "temperature_2m": "temp_c",
    "relative_humidity_2m": "humidity_pct",
    "cloud_cover": "cloud_pct",
    "shortwave_radiation": "shortwave_wm2",
    "direct_radiation": "direct_wm2",
    "diffuse_radiation": "diffuse_wm2",
    "precipitation": "precipitation_mm",
    "rain": "rain_mm",
    "snowfall": "snowfall_cm",          # cm of snow; roughly / 7 for mm of water
    "wind_speed_10m": "wind_speed",     # m/s, to match OWM (wind_speed_unit=ms)
    "wind_gusts_10m": "wind_gust",
    "wind_direction_10m": "wind_deg",
}


class OpenMeteoError(RuntimeError):
    pass


class OpenMeteo:
    def __init__(self, timeout=30):
        self._timeout = timeout

    def _get(self, url, params):
        query = urllib.parse.urlencode(dict(params, **{
            "hourly": ",".join(VARIABLES),
            "timezone": "GMT",           # UTC hour labels, like everything else here
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
        }))
        try:
            with urllib.request.urlopen(url + "?" + query, timeout=self._timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            # Open-Meteo puts a readable reason in the body ({"error": true, "reason": ...}).
            try:
                reason = json.load(exc).get("reason")
            except Exception:
                reason = None
            raise OpenMeteoError("open-meteo HTTP %s%s" % (
                exc.code, ": %s" % reason if reason else "")) from None
        except Exception as exc:
            raise OpenMeteoError("open-meteo request failed: %s" % type(exc).__name__) from None

    def archive(self, lat, lon, start_day, end_day):
        """Hours from start_day 00:00 to end_day 23:00 UTC (YYYY-MM-DD)."""
        body = self._get(ARCHIVE_URL, {
            "latitude": lat, "longitude": lon,
            "start_date": start_day, "end_date": end_day,
        })
        return parse(body)

    def recent(self, lat, lon, past_days, now=None):
        """The last `past_days` days up to now, from the forecast API.

        The forecast API also returns the rest of today, which is forecast, not
        weather that happened, so hours after `now` are dropped.
        """
        body = self._get(FORECAST_URL, {
            "latitude": lat, "longitude": lon,
            "past_days": past_days, "forecast_days": 1,
        })
        return parse(body, not_after=now or datetime.now(timezone.utc))


def parse(body, not_after=None):
    """Turn an Open-Meteo response into weather_hourly rows."""
    if not isinstance(body, dict) or body.get("error"):
        raise OpenMeteoError("open-meteo error: %s" % (
            body.get("reason") if isinstance(body, dict) else "bad response"))
    if body.get("utc_offset_seconds", 0) != 0:
        # Guard the one thing that would silently shift every hour.
        raise OpenMeteoError("open-meteo returned local time, expected GMT")
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise OpenMeteoError("open-meteo response contained no hours")

    columns = {}
    for var, col in VARIABLES.items():
        values = hourly.get(var)
        if values is None:
            raise OpenMeteoError("open-meteo response is missing %s" % var)
        if len(values) != len(times):
            raise OpenMeteoError("open-meteo %s has %d values for %d hours"
                                 % (var, len(values), len(times)))
        columns[col] = values

    rows = []
    for i, label in enumerate(times):
        # "2026-10-10T13:00" -> "2026-10-10T13:00:00Z"
        ts = datetime.strptime(label, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        if not_after is not None and ts > not_after:
            continue
        row = {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}
        for col, values in columns.items():
            row[col] = values[i]
        rows.append(row)
    return rows


# The archive is asked for nothing newer than this many days ago. It runs
# several days behind anyway, and the recent days come from the forecast API,
# so stopping short loses nothing and avoids asking for dates it may refuse.
ARCHIVE_LAG_DAYS = 2


def sync(store, client, lat, lon, start_day, now, recent_days=7):
    """Bring weather_hourly up to date. Returns a summary; raises OpenMeteoError.

    1. Archive: from the first day (on or after start_day) that isn't complete,
       up to ARCHIVE_LAG_DAYS ago. Days the archive hasn't reached yet come back
       as nulls and are skipped, so they're asked for again next time.
    2. Recent: the last `recent_days` from the forecast API, filling the days
       the archive hasn't reached. It never overwrites archive hours.

    The two halves are independent: if the archive fails, the recent days are
    still fetched, and the archive error is raised afterwards.
    """
    fetched = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    last_day = (now - timedelta(days=ARCHIVE_LAG_DAYS)).date().isoformat()
    archived, archive_error = 0, None
    try:
        day = store.first_archive_gap_day(start_day, last_day) if start_day <= last_day else None
        while day is not None and day <= last_day:
            end = min(
                (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=MAX_ARCHIVE_DAYS - 1)).date().isoformat(),
                last_day,
            )
            archived += store.record_weather_hourly(client.archive(lat, lon, day, end), "archive", fetched)
            store.commit()
            day = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
    except OpenMeteoError as exc:
        archive_error = exc

    recent = store.record_weather_hourly(client.recent(lat, lon, recent_days, now), "forecast", fetched)
    store.commit()
    if archive_error is not None:
        raise OpenMeteoError("archive: %s (recent days saved: %d)" % (archive_error, recent))
    log.info("open-meteo ok: %d archive hour(s), %d recent hour(s)", archived, recent)
    return {"archive_rows": archived, "recent_rows": recent}
