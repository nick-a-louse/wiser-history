"""TECH-103: half-hourly gas and electricity from the smart meter via Bright.

No network: a fake Glowmarkt account that behaves like the real one
(10-day limit per request, nulls for missing half-hours, data lagging a bit).
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from collector import glow, main, weather  # noqa: E402
from collector.config import Config, ConfigError  # noqa: E402
from collector.store import Store  # noqa: E402

NOW = datetime(2026, 10, 22, 18, 40, tzinfo=timezone.utc)
CLASSIFIERS = ["gas.consumption", "electricity.consumption"]


class FakeBright:
    """Meter data exists from `history_from` to `lag` before now."""

    def __init__(self, history_from=None, lag=timedelta(hours=2), resources=CLASSIFIERS,
                 fail_login=False, holes=()):
        self.history_from = history_from
        self.holes = list(holes)          # [(start, end)] Bright doesn't have (yet)
        self.lag = lag
        self.available = {c: "id-" + c for c in resources}
        self.fail_login = fail_login
        self.calls = []

    def login(self):
        self.calls.append(("login",))
        if self.fail_login:
            raise glow.GlowError("glow auth: HTTP 401")
        return "tok"

    def resources(self, token):
        return dict(self.available)

    def catchup(self, token, rid):
        self.calls.append(("catchup", rid))

    def readings(self, token, rid, start, end):
        self.calls.append(("readings", rid, start, end))
        if end - start > timedelta(days=glow.CHUNK_DAYS):
            raise glow.GlowError("glow resource: HTTP 400")       # like the real API at 31 days
        rows, t = [], start
        while t < end:
            has = (self.history_from is not None and self.history_from <= t <= NOW - self.lag
                   and not any(a <= t < b for a, b in self.holes))
            value = (0.0 if t.hour < 5 else 1.25) if has else None   # genuine zeros overnight
            rows.append((t.strftime("%Y-%m-%dT%H:%M:%SZ"), value))
            t += timedelta(minutes=30)
        return "kWh", rows

    def readings_calls(self):
        return [c for c in self.calls if c[0] == "readings"]


def count(store, classifier="gas.consumption"):
    return store.conn.execute(
        "SELECT COUNT(*) FROM energy_halfhourly WHERE classifier=?", (classifier,)).fetchone()[0]


# ------------------------------------------------------------------ client parsing

def test_readings_keep_nulls_and_convert_epochs(monkeypatch):
    client = glow.Glow("user", "pw")
    t0 = int(datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(client, "_call", lambda *a, **k: {
        "units": "kWh", "data": [[t0, 0.0], [t0 + 1800, 1.5], [t0 + 3600, None]]})
    units, rows = client.readings("tok", "rid", NOW - timedelta(days=1), NOW)
    assert units == "kWh"
    assert rows == [("2026-10-20T00:00:00Z", 0.0), ("2026-10-20T00:30:00Z", 1.5),
                    ("2026-10-20T01:00:00Z", None)]


def test_store_skips_missing_but_keeps_real_zeros(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    n = store.record_energy("gas.consumption",
                            [("2026-10-20T00:00:00Z", 0.0), ("2026-10-20T00:30:00Z", None)], "kWh", "t")
    assert n == 1 and count(store) == 1


# ------------------------------------------------------------------ sync

def test_no_data_yet_costs_one_request_per_meter(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=None)
    assert glow.sync(store, bright, CLASSIFIERS, 395, NOW) == {c: 0 for c in CLASSIFIERS}
    assert len(bright.readings_calls()) == 2


def test_full_backfill_then_cheap_refreshes(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=datetime(2025, 9, 1, tzinfo=timezone.utc))
    glow.sync(store, bright, CLASSIFIERS, 395, NOW)

    first, last = store.energy_range("gas.consumption")
    window_start = NOW - timedelta(days=395)
    assert first <= (window_start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert last == "2026-10-22T16:30:00Z"                      # up to the meter's lag
    assert all(c[3] - c[2] <= timedelta(days=10) for c in bright.readings_calls())
    # Every half-hour in the window is there, once.
    expected = int((datetime(2026, 10, 22, 16, 30, tzinfo=timezone.utc) - datetime.strptime(
        first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() // 1800) + 1
    assert count(store) == expected
    assert store.conn.execute("SELECT MIN(value) FROM energy_halfhourly").fetchone()[0] == 0.0

    bright.calls.clear()
    glow.sync(store, bright, CLASSIFIERS, 395, NOW + timedelta(hours=6))
    # Only the recent refresh, per meter: nothing older is asked for again.
    assert len(bright.readings_calls()) == 2


def test_short_history_stops_after_two_empty_chunks(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=NOW - timedelta(days=100))     # meter fitted 100 days ago
    glow.sync(store, bright, ["gas.consumption"], 395, NOW)
    first, _ = store.energy_range("gas.consumption")
    assert first[:10] == (NOW - timedelta(days=100)).strftime("%Y-%m-%d")
    # 1 recent + ~10 back-fill chunks with data + 2 empty, not all 40.
    assert len(bright.readings_calls()) <= 14

    bright.calls.clear()
    glow.sync(store, bright, ["gas.consumption"], 395, NOW + timedelta(hours=6))
    assert len(bright.readings_calls()) <= 4                       # refresh + 2 empty probes


def test_history_arriving_later_is_picked_up(tmp_path):
    """Bright may show recent data first and fetch older history afterwards."""
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=NOW - timedelta(days=5))
    glow.sync(store, bright, ["gas.consumption"], 395, NOW)
    bright.history_from = NOW - timedelta(days=60)
    glow.sync(store, bright, ["gas.consumption"], 395, NOW + timedelta(hours=6))
    first, _ = store.energy_range("gas.consumption")
    assert first[:10] == (NOW - timedelta(days=60)).strftime("%Y-%m-%d")


def test_missing_meter_is_skipped_but_none_at_all_is_an_error(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    gas_only = FakeBright(history_from=NOW - timedelta(days=2), resources=["gas.consumption"])
    assert set(glow.sync(store, gas_only, CLASSIFIERS, 395, NOW)) == {"gas.consumption"}
    with pytest.raises(glow.GlowError, match="none of"):
        glow.sync(store, FakeBright(resources=[]), CLASSIFIERS, 395, NOW)


# ------------------------------------------------------------------ collector

class FakeHub:
    def fetch(self):
        return {"System": {"GeoPosition": {"Latitude": 51.0, "Longitude": 0.0}},
                "HeatingChannel": [{}], "Room": [{"id": 1, "Name": "Lounge", "CalculatedTemperature": 195}]}


class Cfg:
    poll_seconds, weather_every, s3_every, db_backup_every = 300, 99, 12, 288
    openmeteo_enabled = False
    glow_enabled, glow_every_hours, glow_history_days = True, 6, 395
    glow_classifiers = CLASSIFIERS

    def __init__(self, tmp):
        self.publish_dir = str(tmp / "published")


@pytest.fixture
def fresh_state(monkeypatch):
    for k, v in {"glow_next_epoch": 0.0, "last_glow_ok": 0, "glow_last_success_epoch": 0.0,
                 "glow_latest": {}, "failures": 0}.items():
        monkeypatch.setitem(main.STATE, k, v)


def test_bright_outage_costs_no_hub_reading(tmp_path, fresh_state):
    store = Store(str(tmp_path / "w.db"))
    main.cycle(Cfg(tmp_path), store, FakeHub(), None, None, 1, None, FakeBright(fail_login=True))
    run = store.conn.execute("SELECT hub_ok, glow_ok, error FROM run_log").fetchone()
    assert (run["hub_ok"], run["glow_ok"]) == (1, 0) and "HTTP 401" in run["error"]
    assert store.conn.execute("SELECT COUNT(*) FROM room_reading").fetchone()[0] == 1
    assert 3000 < main.STATE["glow_next_epoch"] - time.time() <= 3600


def test_sync_runs_then_waits_and_metrics_show_it(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setattr(main, "utcnow", lambda: NOW)
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=NOW - timedelta(days=3))
    main.cycle(Cfg(tmp_path), store, FakeHub(), None, None, 1, None, bright)
    assert main.STATE["last_glow_ok"] == 1
    n = len(bright.calls)
    main.cycle(Cfg(tmp_path), store, FakeHub(), None, None, 2, None, bright)
    assert len(bright.calls) == n                                   # not due for 6 hours

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
    assert "wiser_glow_up 1" in body
    assert 'wiser_glow_latest_reading_timestamp_seconds{classifier="gas.consumption"} 17' in body


# ------------------------------------------------------------------ config and OWM

def test_bright_login_needs_both_settings(monkeypatch):
    for k, v in {"WISER_HUB_IP": "h", "WISER_SECRET": "s", "OWM_API_KEY": "k", "S3_BUCKET": "b"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GLOW_PASSWORD", raising=False)
    monkeypatch.delenv("GLOW_USERNAME", raising=False)
    assert Config().glow_enabled is False
    monkeypatch.setenv("GLOW_USERNAME", "someone@example.org")
    with pytest.raises(ConfigError, match="both"):
        Config()
    monkeypatch.setenv("GLOW_PASSWORD", "pw")
    cfg = Config()
    assert cfg.glow_enabled and "someone@" not in repr(cfg) and "pw" not in repr(cfg)


def test_owm_error_names_the_http_status(monkeypatch):
    import urllib.error

    def refuse(*a, **k):
        raise urllib.error.HTTPError("https://example.invalid/?appid=SECRET", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(weather.urllib.request, "urlopen", refuse)
    with pytest.raises(weather.WeatherError) as exc:
        weather.Weather("SECRET").current(51.0, 0.0)
    assert str(exc.value) == "weather request failed: HTTP 401" and "SECRET" not in str(exc.value)


def test_new_database_has_glow_column(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    assert "glow_ok" in {r[1] for r in store.conn.execute("PRAGMA table_info(run_log)")}


# ------------------------------------------------------------------ gap-filling

def missing(store, now=NOW):
    return glow.missing_halfhours(store, ["gas.consumption"], now)["gas.consumption"]


def test_holes_filled_once_bright_has_them(tmp_path):
    """The bug seen live on 2026-10-29: history read once with holes stayed holed."""
    store = Store(str(tmp_path / "w.db"))
    hole = (NOW - timedelta(days=40), NOW - timedelta(days=25))
    bright = FakeBright(history_from=NOW - timedelta(days=60), holes=[hole])
    glow.sync(store, bright, ["gas.consumption"], 395, NOW)
    assert missing(store) == 15 * 48                     # 15 days absent, and known to be absent

    bright.holes = []                                    # Bright fetches them from the DCC later
    glow.sync(store, bright, ["gas.consumption"], 395, NOW + timedelta(hours=6))
    assert missing(store, NOW + timedelta(hours=6)) == 0


def test_complete_chunks_are_never_reread(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=NOW - timedelta(days=60))
    glow.sync(store, bright, ["gas.consumption"], 395, NOW)
    bright.calls.clear()
    glow.sync(store, bright, ["gas.consumption"], 395, NOW + timedelta(hours=6))
    # refresh + 2 empty probes below the oldest reading; no gap re-reads
    assert len(bright.readings_calls()) <= 3


def test_gap_rereads_are_capped_per_sync(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    # Every other day missing across ~6 months: lots of incomplete chunks.
    start = NOW - timedelta(days=180)
    holes = [(start + timedelta(days=d), start + timedelta(days=d + 1)) for d in range(0, 175, 2)]
    bright = FakeBright(history_from=start, holes=holes)
    glow.sync(store, bright, ["gas.consumption"], 395, NOW)
    bright.calls.clear()
    glow.sync(store, bright, ["gas.consumption"], 395, NOW + timedelta(hours=6))
    rereads = len(bright.readings_calls())
    assert rereads <= 1 + glow.EMPTY_CHUNKS_TO_STOP + glow.GAP_CHUNKS_PER_SYNC


def test_rereads_rotate_through_every_incomplete_chunk(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    start = NOW - timedelta(days=180)
    holes = [(start + timedelta(days=d), start + timedelta(days=d + 1)) for d in range(0, 175, 2)]
    bright = FakeBright(history_from=start, holes=holes)
    t = NOW
    glow.sync(store, bright, ["gas.consumption"], 395, t)
    before = missing(store, t)
    bright.holes = []
    for _ in range(4):                                   # ~18 incomplete chunks, 6 per sync
        t += timedelta(hours=6)
        glow.sync(store, bright, ["gas.consumption"], 395, t)
    assert before > 0 and missing(store, t) == 0


def test_old_holes_that_never_fill_are_given_up(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    hole = (NOW - timedelta(days=100), NOW - timedelta(days=99))      # meter offline that day
    bright = FakeBright(history_from=NOW - timedelta(days=120), holes=[hole])
    t = NOW
    for _ in range(glow.GAP_MAX_TRIES + 3):
        glow.sync(store, bright, ["gas.consumption"], 395, t)
        t += timedelta(hours=6)
    tried = store.energy_chunks("gas.consumption")
    assert max(f for f, _ in tried.values()) == glow.GAP_MAX_TRIES     # stopped asking
    bright.calls.clear()
    glow.sync(store, bright, ["gas.consumption"], 395, t)
    assert len(bright.readings_calls()) <= 3
    assert missing(store, t) == 48                                     # still reported, honestly


def test_missing_metric_is_published(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setattr(main, "utcnow", lambda: NOW)
    store = Store(str(tmp_path / "w.db"))
    bright = FakeBright(history_from=NOW - timedelta(days=30),
                        holes=[(NOW - timedelta(days=20), NOW - timedelta(days=19))])
    main.cycle(Cfg(tmp_path), store, FakeHub(), None, None, 1, None, bright)
    assert main.STATE["glow_missing"] == {"gas.consumption": 48, "electricity.consumption": 48}
