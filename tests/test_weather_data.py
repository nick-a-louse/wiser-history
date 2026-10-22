"""TECH-97: rain and snow amounts from OWM, and the Open-Meteo hourly back-fill.

No network: responses are built in the shapes the two APIs document.
Coordinates are made up.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from collector import main, openmeteo, weather  # noqa: E402
from collector.store import Store  # noqa: E402

LAT, LON = 51.0, 0.0


# ------------------------------------------------------------------ OWM

def owm_current(**extra):
    cur = {"dt": 1777449371, "temp": 9.5, "humidity": 81, "clouds": 90,
           "wind_speed": 6.1, "weather": [{"id": 500, "main": "Rain", "description": "light rain"}]}
    cur.update(extra)
    return cur


def test_owm_rain_and_snow_amounts_are_stored():
    row = weather._row(owm_current(rain={"1h": 0.82}, snow={"1h": 0.1}))
    assert row["rain_1h_mm"] == 0.82 and row["snow_1h_mm"] == 0.1
    assert row["condition_id"] == 500 and row["condition"] == "Rain"


def test_owm_dry_hour_is_zero_not_missing():
    # OWM leaves the keys out entirely when nothing fell.
    row = weather._row(owm_current(weather=[{"id": 800, "main": "Clear"}]))
    assert row["rain_1h_mm"] == 0.0 and row["snow_1h_mm"] == 0.0


def test_owm_row_still_fits_the_table(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    store.record_weather("2026-10-22T12:00:00Z", weather._row(owm_current(rain={"1h": 1.2})))
    got = store.conn.execute("SELECT rain_1h_mm, snow_1h_mm, condition_id FROM weather").fetchone()
    assert tuple(got) == (1.2, 0.0, 500)


# ------------------------------------------------------------------ migration

OLD_SCHEMA = """
CREATE TABLE weather (ts_utc TEXT PRIMARY KEY, observed_unix INTEGER, temp_c REAL,
  feels_like_c REAL, humidity_pct INTEGER, dew_point_c REAL, pressure_hpa INTEGER,
  clouds_pct INTEGER, wind_speed REAL, wind_gust REAL, wind_deg INTEGER, uvi REAL,
  visibility INTEGER, condition TEXT, description TEXT, sunrise_unix INTEGER, sunset_unix INTEGER);
CREATE TABLE run_log (ts_utc TEXT PRIMARY KEY, hub_ok INTEGER NOT NULL, weather_ok INTEGER,
  publish_ok INTEGER, duration_ms INTEGER, error TEXT);
INSERT INTO weather (ts_utc, temp_c, condition) VALUES ('2026-08-04T10:00:00Z', 18.5, 'Rain');
INSERT INTO run_log (ts_utc, hub_ok) VALUES ('2026-08-04T10:00:00Z', 1);
"""


def test_existing_database_is_migrated_without_losing_rows(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    store = Store(path)                       # runs the migration
    Store(path).close()                       # and running it twice is harmless
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(weather)")}
    assert {"rain_1h_mm", "snow_1h_mm", "condition_id"} <= cols
    assert "openmeteo_ok" in {r[1] for r in store.conn.execute("PRAGMA table_info(run_log)")}
    old = store.conn.execute("SELECT temp_c, condition, rain_1h_mm FROM weather").fetchone()
    assert tuple(old) == (18.5, "Rain", None)  # history keeps NULL: nobody measured it then
    assert store.conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 1


# ------------------------------------------------------------------ Open-Meteo parsing

def om_response(start="2026-10-20T00:00", hours=24, value=1.0, nulls_from=None):
    """A response in Open-Meteo's documented shape (timezone=GMT)."""
    t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M")
    times = [(t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)]
    hourly = {"time": times}
    for var in openmeteo.VARIABLES:
        vals = [value] * hours
        if nulls_from is not None:
            vals[nulls_from:] = [None] * (hours - nulls_from)
        hourly[var] = vals
    return {"latitude": LAT, "longitude": LON, "utc_offset_seconds": 0, "timezone": "GMT",
            "hourly_units": {"time": "iso8601"}, "hourly": hourly}


def test_parse_maps_variables_and_utc_labels():
    rows = openmeteo.parse(om_response(hours=3, value=2.5))
    assert [r["ts_utc"] for r in rows] == ["2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z", "2026-10-20T02:00:00Z"]
    assert rows[0]["shortwave_wm2"] == 2.5 and rows[0]["snowfall_cm"] == 2.5 and rows[0]["wind_speed"] == 2.5


def test_parse_drops_hours_that_have_not_happened():
    now = datetime(2026, 10, 20, 13, 40, tzinfo=timezone.utc)
    rows = openmeteo.parse(om_response(hours=24), not_after=now)
    # 13:00 covers 12:00-13:00 and has happened; 14:00 is still a forecast.
    assert rows[-1]["ts_utc"] == "2026-10-20T13:00:00Z"


@pytest.mark.parametrize("body, message", [
    ({"error": True, "reason": "Parameter 'start_date' is out of allowed range"}, "out of allowed range"),
    (dict(om_response(hours=2), utc_offset_seconds=3600), "local time"),
    ({"utc_offset_seconds": 0, "hourly": {"time": []}}, "no hours"),
])
def test_parse_rejects_bad_responses(body, message):
    with pytest.raises(openmeteo.OpenMeteoError, match=message):
        openmeteo.parse(body)


def test_parse_rejects_a_missing_variable():
    body = om_response(hours=2)
    del body["hourly"]["direct_radiation"]
    with pytest.raises(openmeteo.OpenMeteoError, match="direct_radiation"):
        openmeteo.parse(body)


# ------------------------------------------------------------------ store rules

def test_archive_beats_forecast_and_empty_hours_are_skipped(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    store.record_weather_hourly(openmeteo.parse(om_response(hours=2, value=1.0)), "forecast", "t1")
    store.record_weather_hourly(openmeteo.parse(om_response(hours=2, value=2.0, nulls_from=1)), "archive", "t2")
    store.record_weather_hourly(openmeteo.parse(om_response(hours=2, value=3.0)), "forecast", "t3")
    rows = [tuple(r) for r in store.conn.execute(
        "SELECT ts_utc, source, temp_c FROM weather_hourly ORDER BY ts_utc")]
    assert rows == [
        ("2026-10-20T00:00:00Z", "archive", 2.0),   # archive replaced forecast, then kept
        ("2026-10-20T01:00:00Z", "forecast", 3.0),  # archive had nulls here, so forecast stays and updates
    ]


def test_first_archive_gap_day(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    assert store.first_archive_gap_day("2026-10-19", "2026-10-21") == "2026-10-19"
    store.record_weather_hourly(openmeteo.parse(om_response("2026-10-19T00:00", hours=48)), "archive", "t")
    assert store.first_archive_gap_day("2026-10-19", "2026-10-21") == "2026-10-21"
    assert store.first_archive_gap_day("2026-10-19", "2026-10-20") is None


# ------------------------------------------------------------------ sync

class FakeOpenMeteo:
    """Answers like Open-Meteo: archive nulls for the last few days, as ERA5 does."""
    def __init__(self, archive_lag_days=5, fail=False, archive_fails=False):
        self.lag, self.fail, self.calls = archive_lag_days, fail, []
        self.archive_fails = archive_fails

    def archive(self, lat, lon, start_day, end_day):
        self.calls.append(("archive", start_day, end_day))
        if self.fail or self.archive_fails:
            raise openmeteo.OpenMeteoError("open-meteo request failed: URLError")
        d0 = datetime.strptime(start_day, "%Y-%m-%d")
        days = (datetime.strptime(end_day, "%Y-%m-%d") - d0).days + 1
        available = max(0, days - self.lag)
        return openmeteo.parse(om_response(d0.strftime("%Y-%m-%dT00:00"), days * 24, 1.0,
                                           nulls_from=available * 24 if available < days else None))

    def recent(self, lat, lon, past_days, now):
        self.calls.append(("recent", past_days))
        if self.fail:
            raise openmeteo.OpenMeteoError("open-meteo request failed: URLError")
        start = (now - timedelta(days=past_days)).strftime("%Y-%m-%dT00:00")
        return openmeteo.parse(om_response(start, (past_days + 1) * 24, 5.0), not_after=now)


def test_sync_backfills_then_only_asks_for_what_is_missing(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    client = FakeOpenMeteo()
    now = datetime(2026, 10, 22, 15, 30, tzinfo=timezone.utc)
    openmeteo.sync(store, client, LAT, LON, "2026-08-04", now)

    archive_calls = [c for c in client.calls if c[0] == "archive"]
    assert archive_calls == [("archive", "2026-08-04", "2026-10-20")]      # up to 2 days ago, one request
    cov = store.hourly_coverage()
    assert cov["archive"]["first"] == "2026-08-04T00:00:00Z"
    assert cov["archive"]["last"] == "2026-10-15T23:00:00Z"                # the fake archive is 5 days behind its end
    assert cov["forecast"]["last"] == "2026-10-22T15:00:00Z"               # nothing after now

    client.calls.clear()
    openmeteo.sync(store, client, LAT, LON, "2026-08-04", now)
    assert client.calls[0] == ("archive", "2026-10-16", "2026-10-20")       # resumes at the first gap


def test_archive_failure_still_saves_the_recent_days(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    now = datetime(2026, 10, 22, 15, 30, tzinfo=timezone.utc)
    with pytest.raises(openmeteo.OpenMeteoError, match="archive: .*recent days saved: 1"):
        openmeteo.sync(store, FakeOpenMeteo(archive_fails=True), LAT, LON, "2026-08-04", now)
    cov = store.hourly_coverage()
    assert "archive" not in cov and cov["forecast"]["last"] == "2026-10-22T15:00:00Z"


def test_long_backfill_is_split_into_chunks(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    client = FakeOpenMeteo(archive_lag_days=0)
    openmeteo.sync(store, client, LAT, LON, "2026-01-01", datetime(2026, 10, 22, tzinfo=timezone.utc))
    spans = [c for c in client.calls if c[0] == "archive"]
    assert len(spans) == 4 and spans[0] == ("archive", "2026-01-01", "2026-04-02")
    assert spans[-1][2] == "2026-10-20"


# ------------------------------------------------------------------ collector cycle

class FakeHub:
    def fetch(self):
        return {"System": {"GeoPosition": {"Latitude": LAT, "Longitude": LON}, "UnixTime": 1},
                "HeatingChannel": [{"HeatingRelayState": "Off", "PercentageDemand": 0}],
                "Room": [{"id": 1, "Name": "Lounge", "CalculatedTemperature": 195}]}


class FakeWeather:
    def current(self, lat, lon):
        return weather._row(owm_current(rain={"1h": 0.4}))


class Cfg:
    poll_seconds, weather_every, s3_every, db_backup_every = 300, 1, 12, 288
    openmeteo_enabled, openmeteo_every_hours, openmeteo_recent_days = True, 6, 7
    openmeteo_start = None

    def __init__(self, tmp):
        self.publish_dir = str(tmp / "published")


@pytest.fixture
def fresh_state(monkeypatch):
    monkeypatch.setitem(main.STATE, "openmeteo_next_epoch", 0.0)
    monkeypatch.setitem(main.STATE, "last_openmeteo_ok", 0)
    monkeypatch.setitem(main.STATE, "failures", 0)


def test_openmeteo_outage_costs_no_hub_reading(tmp_path, fresh_state):
    store = Store(str(tmp_path / "w.db"))
    main.cycle(Cfg(tmp_path), store, FakeHub(), FakeWeather(), None, 0, FakeOpenMeteo(fail=True))
    run = store.conn.execute("SELECT hub_ok, weather_ok, openmeteo_ok, error FROM run_log").fetchone()
    assert (run["hub_ok"], run["weather_ok"], run["openmeteo_ok"]) == (1, 1, 0)
    assert "open-meteo" in run["error"]
    assert store.conn.execute("SELECT COUNT(*) FROM room_reading").fetchone()[0] == 1
    assert store.conn.execute("SELECT rain_1h_mm FROM weather").fetchone()[0] == 0.4
    assert main.STATE["last_openmeteo_ok"] == 0
    # Retries in an hour, not every 5 minutes.
    assert 3000 < main.STATE["openmeteo_next_epoch"] - time.time() <= 3600


def test_openmeteo_runs_once_then_waits(tmp_path, fresh_state):
    store = Store(str(tmp_path / "w.db"))
    client = FakeOpenMeteo()
    main.cycle(Cfg(tmp_path), store, FakeHub(), FakeWeather(), None, 0, client)
    assert main.STATE["last_openmeteo_ok"] == 1
    assert store.conn.execute("SELECT openmeteo_ok FROM run_log").fetchone()[0] == 1
    n = len(client.calls)
    main.cycle(Cfg(tmp_path), store, FakeHub(), FakeWeather(), None, 1, client)
    assert len(client.calls) == n                   # not due again for 6 hours
    assert store.conn.execute("SELECT openmeteo_ok FROM run_log ORDER BY ts_utc DESC").fetchone()[0] is None


def test_metrics_include_openmeteo():
    import io

    class H(main.MetricsHandler):
        def __init__(self):
            self.path, self.wfile = "/metrics", io.BytesIO()
        def send_response(self, *a): pass
        def send_header(self, *a): pass
        def end_headers(self): pass

    h = H()
    h.do_GET()
    body = h.wfile.getvalue().decode()
    assert "wiser_openmeteo_up " in body and "wiser_openmeteo_last_success_timestamp_seconds " in body
