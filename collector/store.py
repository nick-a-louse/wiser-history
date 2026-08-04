"""SQLite store - the source of truth.

Design notes worth keeping in view:

* Every timestamp is UTC. The hub reports `TimeZoneOffset: 0` while sitting in
  BST, so any local-time scheme is an hour wrong for half the year - which is
  exactly how the original project's logDate/logTime schema broke.
* Rooms are keyed on the hub's numeric id, never on name. Renames then cost
  nothing; keying on name would silently fragment the history.
"""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS room (
    room_id        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc  TEXT NOT NULL
);

-- Append-only. Nothing queries this; it exists because past names cannot be
-- recovered later if they are not written down as they change.
CREATE TABLE IF NOT EXISTS room_rename_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id        INTEGER NOT NULL,
    old_name       TEXT NOT NULL,
    new_name       TEXT NOT NULL,
    changed_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sample (
    ts_utc        TEXT PRIMARY KEY,
    hub_unix_time INTEGER,
    boiler_relay  TEXT,
    boiler_demand INTEGER,
    system_mode   TEXT,
    away_mode     INTEGER
);

CREATE TABLE IF NOT EXISTS room_reading (
    ts_utc               TEXT NOT NULL,
    room_id              INTEGER NOT NULL,
    temp_c               REAL,
    temp_source          TEXT,
    setpoint_c           REAL,
    scheduled_setpoint_c REAL,
    demand_pct           INTEGER,
    output_state         TEXT,
    mode                 TEXT,
    effective_mode       TEXT,
    setpoint_origin      TEXT,
    window_state         TEXT,
    humidity_pct         INTEGER,
    heating_rate         INTEGER,
    comfort_score        INTEGER,
    PRIMARY KEY (ts_utc, room_id)
);

CREATE INDEX IF NOT EXISTS idx_room_reading_room_ts
    ON room_reading (room_id, ts_utc);

CREATE TABLE IF NOT EXISTS device_reading (
    ts_utc          TEXT NOT NULL,
    device_id       INTEGER NOT NULL,
    room_id         INTEGER,
    product_type    TEXT,
    temp_c          REAL,
    humidity_pct    INTEGER,
    setpoint_c      REAL,
    demand_pct      INTEGER,
    battery_level   TEXT,
    battery_voltage REAL,
    signal          TEXT,
    PRIMARY KEY (ts_utc, device_id)
);

CREATE TABLE IF NOT EXISTS weather (
    ts_utc        TEXT PRIMARY KEY,
    observed_unix INTEGER,
    temp_c        REAL,
    feels_like_c  REAL,
    humidity_pct  INTEGER,
    dew_point_c   REAL,
    pressure_hpa  INTEGER,
    clouds_pct    INTEGER,
    wind_speed    REAL,
    wind_gust     REAL,
    wind_deg      INTEGER,
    uvi           REAL,
    visibility    INTEGER,
    condition     TEXT,
    description   TEXT,
    sunrise_unix  INTEGER,
    sunset_unix   INTEGER
);

-- Every cycle writes a row here, successful or not. This is what makes a
-- silent death visible: the original failed for years because nothing recorded
-- that it had stopped producing data.
CREATE TABLE IF NOT EXISTS run_log (
    ts_utc      TEXT PRIMARY KEY,
    hub_ok      INTEGER NOT NULL,
    weather_ok  INTEGER,
    publish_ok  INTEGER,
    duration_ms INTEGER,
    error       TEXT
);
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(conn, table, row):
    cols = list(row.keys())
    conn.execute(
        "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
        % (table, ",".join(cols), ",".join("?" * len(cols))),
        [row[c] for c in cols],
    )


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- writes ---------------------------------------------------------

    def record_rooms(self, ts, rooms):
        """Upsert room identity, logging any rename rather than losing it."""
        for room in rooms:
            rid, name = room["room_id"], room["name"]
            cur = self.conn.execute(
                "SELECT name FROM room WHERE room_id=?", (rid,)
            ).fetchone()
            if cur is None:
                self.conn.execute(
                    "INSERT INTO room (room_id, name, first_seen_utc, last_seen_utc)"
                    " VALUES (?,?,?,?)",
                    (rid, name, ts, ts),
                )
            else:
                if cur["name"] != name:
                    self.conn.execute(
                        "INSERT INTO room_rename_log"
                        " (room_id, old_name, new_name, changed_at_utc)"
                        " VALUES (?,?,?,?)",
                        (rid, cur["name"], name, ts),
                    )
                    self.conn.execute(
                        "UPDATE room SET name=? WHERE room_id=?", (name, rid)
                    )
                self.conn.execute(
                    "UPDATE room SET last_seen_utc=? WHERE room_id=?", (ts, rid)
                )

    def record_sample(self, ts, system, rooms, devices):
        row = dict(system)
        row["ts_utc"] = ts
        _insert(self.conn, "sample", row)

        for room in rooms:
            r = {k: v for k, v in room.items() if k != "name"}
            r["ts_utc"] = ts
            _insert(self.conn, "room_reading", r)

        for dev in devices:
            d = dict(dev)
            d["ts_utc"] = ts
            _insert(self.conn, "device_reading", d)

    def record_weather(self, ts, row):
        r = dict(row)
        r["ts_utc"] = ts
        _insert(self.conn, "weather", r)

    def record_run(self, ts, hub_ok, weather_ok, publish_ok, duration_ms, error=None):
        _insert(self.conn, "run_log", {
            "ts_utc": ts,
            "hub_ok": int(hub_ok),
            "weather_ok": None if weather_ok is None else int(weather_ok),
            "publish_ok": None if publish_ok is None else int(publish_ok),
            "duration_ms": duration_ms,
            "error": error,
        })

    def commit(self):
        self.conn.commit()

    # -- reads ----------------------------------------------------------

    def rooms(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT room_id, name, first_seen_utc, last_seen_utc"
            " FROM room ORDER BY name"
        )]

    def renames(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT room_id, old_name, new_name, changed_at_utc"
            " FROM room_rename_log ORDER BY changed_at_utc"
        )]

    def coverage(self):
        row = self.conn.execute(
            "SELECT MIN(ts_utc) AS first, MAX(ts_utc) AS last, COUNT(*) AS n"
            " FROM sample"
        ).fetchone()
        return dict(row) if row else {"first": None, "last": None, "n": 0}

    def last_success(self):
        row = self.conn.execute(
            "SELECT MAX(ts_utc) AS ts FROM run_log WHERE hub_ok=1"
        ).fetchone()
        return row["ts"] if row else None

    def months(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT substr(ts_utc,1,7) FROM sample ORDER BY 1"
        )]

    def raw_month(self, month):
        """Native-resolution readings for one YYYY-MM."""
        like = month + "%"
        rooms = self.conn.execute(
            "SELECT ts_utc, room_id, temp_c, setpoint_c, demand_pct,"
            "       humidity_pct, output_state, window_state"
            "  FROM room_reading WHERE ts_utc LIKE ? ORDER BY ts_utc", (like,)
        )
        weather = self.conn.execute(
            "SELECT ts_utc, temp_c, humidity_pct, clouds_pct, wind_speed"
            "  FROM weather WHERE ts_utc LIKE ? ORDER BY ts_utc", (like,)
        )
        boiler = self.conn.execute(
            "SELECT ts_utc, boiler_relay, boiler_demand"
            "  FROM sample WHERE ts_utc LIKE ? ORDER BY ts_utc", (like,)
        )
        return {
            "rooms": [dict(r) for r in rooms],
            "weather": [dict(r) for r in weather],
            "boiler": [dict(r) for r in boiler],
        }

    def rollup_30min(self, year):
        """Half-hourly means, used for mid-range zoom levels."""
        like = str(year) + "%"
        rows = self.conn.execute(
            "SELECT substr(ts_utc,1,14) ||"
            "       CASE WHEN CAST(substr(ts_utc,15,2) AS INTEGER) < 30"
            "            THEN '00' ELSE '30' END || ':00Z' AS bucket,"
            "       room_id, AVG(temp_c) AS temp_c, AVG(setpoint_c) AS setpoint_c,"
            "       AVG(demand_pct) AS demand_pct"
            "  FROM room_reading WHERE ts_utc LIKE ?"
            " GROUP BY bucket, room_id ORDER BY bucket", (like,)
        )
        weather = self.conn.execute(
            "SELECT substr(ts_utc,1,14) ||"
            "       CASE WHEN CAST(substr(ts_utc,15,2) AS INTEGER) < 30"
            "            THEN '00' ELSE '30' END || ':00Z' AS bucket,"
            "       AVG(temp_c) AS temp_c, AVG(humidity_pct) AS humidity_pct,"
            "       AVG(clouds_pct) AS clouds_pct"
            "  FROM weather WHERE ts_utc LIKE ?"
            " GROUP BY bucket ORDER BY bucket", (like,)
        )
        return {"rooms": [dict(r) for r in rows],
                "weather": [dict(r) for r in weather]}

    def rollup_daily(self):
        """Daily min/max/mean across all history - the widest zoom tier."""
        rows = self.conn.execute(
            "SELECT substr(ts_utc,1,10) AS day, room_id,"
            "       MIN(temp_c) AS min_c, MAX(temp_c) AS max_c, AVG(temp_c) AS mean_c,"
            "       AVG(demand_pct) AS demand_pct"
            "  FROM room_reading GROUP BY day, room_id ORDER BY day"
        )
        weather = self.conn.execute(
            "SELECT substr(ts_utc,1,10) AS day,"
            "       MIN(temp_c) AS min_c, MAX(temp_c) AS max_c, AVG(temp_c) AS mean_c,"
            "       AVG(humidity_pct) AS humidity_pct, AVG(clouds_pct) AS clouds_pct"
            "  FROM weather GROUP BY day ORDER BY day"
        )
        return {"rooms": [dict(r) for r in rows],
                "weather": [dict(r) for r in weather]}

    def gaps(self, expected_seconds, limit=500):
        """Find intervals where sampling stopped.

        Gaps are never interpolated away - the site draws them as breaks, so a
        failure looks like a failure. Anything over 2x the expected interval
        counts.
        """
        prev, out = None, []
        for row in self.conn.execute("SELECT ts_utc FROM sample ORDER BY ts_utc"):
            ts = datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%SZ")
            if prev is not None:
                delta = (ts - prev).total_seconds()
                if delta > expected_seconds * 2:
                    out.append({
                        "from": iso(prev.replace(tzinfo=timezone.utc)),
                        "to": iso(ts.replace(tzinfo=timezone.utc)),
                        "seconds": int(delta),
                    })
            prev = ts
        return out[-limit:]
