"""Load the collector's SQLite database into tidy daily tables for modelling.

Reads only (opens the database read-only). Days are UTC days, like every
timestamp in the database; in summer that shifts the day boundary by an hour
from UK time, which barely matters for daily energy totals.

Two rules carried over from the collector:
* Missing data is never filled in. A day counts only if it is complete
  (48 half-hours of energy, 22+ hours of weather); other days are NaN.
* Units are stated in column names (kWh, degC, m/s, kWh/m2).
"""
import sqlite3

import numpy as np
import pandas as pd

GAS = "gas.consumption"
ELECTRICITY = "electricity.consumption"


def connect(db_path):
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)


def daily_energy(conn):
    """One row per UTC day: gas_kwh and elec_kwh on complete days (else NaN),
    plus how many half-hours each had."""
    raw = pd.read_sql_query(
        "SELECT substr(ts_utc,1,10) AS day, classifier, SUM(value) AS kwh, COUNT(*) AS n"
        " FROM energy_halfhourly GROUP BY day, classifier", conn)
    if raw.empty:
        return pd.DataFrame(columns=["gas_kwh", "elec_kwh", "gas_halfhours", "elec_halfhours"])
    out = pd.DataFrame(index=pd.to_datetime(sorted(raw["day"].unique())))
    out.index.name = "day"
    for classifier, name in ((GAS, "gas"), (ELECTRICITY, "elec")):
        part = raw[raw["classifier"] == classifier].copy()
        part.index = pd.to_datetime(part["day"])
        out[name + "_halfhours"] = part["n"].reindex(out.index).fillna(0).astype(int)
        out[name + "_kwh"] = part["kwh"].reindex(out.index).where(out[name + "_halfhours"] >= 48)
    return out


def hourly_weather(conn):
    """Open-Meteo hourly weather (archive where available, else the forecast
    API's recent past), indexed by UTC hour."""
    df = pd.read_sql_query(
        "SELECT ts_utc, temp_c, wind_speed, shortwave_wm2, rain_mm, snowfall_cm, cloud_pct"
        " FROM weather_hourly ORDER BY ts_utc", conn)
    # Stored as "...Z"; kept as naive UTC so it lines up with the daily tables.
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True).dt.tz_convert(None)
    return df.set_index("ts_utc")


def degree_days(hourly_temp, base_c):
    """Heating degree-days per UTC day at a given base temperature, summed hour
    by hour: sum(max(0, base - T)) / 24. Hourly summing catches cold nights on
    days whose average looks mild, which a daily-mean formula misses."""
    deficit = (base_c - hourly_temp).clip(lower=0)
    return deficit.groupby(hourly_temp.index.floor("D")).sum() / 24.0


def daily_weather(hourly):
    """One row per UTC day with 22 or more hours of weather."""
    by_day = hourly.groupby(hourly.index.floor("D"))
    out = pd.DataFrame({
        "temp_mean_c": by_day["temp_c"].mean(),
        "temp_min_c": by_day["temp_c"].min(),
        "temp_max_c": by_day["temp_c"].max(),
        "wind_mean_ms": by_day["wind_speed"].mean(),
        # W/m2 averaged over each hour, so summing hours gives Wh/m2.
        "solar_kwh_m2": by_day["shortwave_wm2"].sum() / 1000.0,
        "rain_mm": by_day["rain_mm"].sum(),
        "snow_cm": by_day["snowfall_cm"].sum(),
        "hours": by_day["temp_c"].count(),
    })
    out.index.name = "day"
    return out[out["hours"] >= 22].drop(columns="hours")


def load_daily(db_path):
    """Daily energy joined to daily weather, plus calendar columns.
    Returns (daily, hourly_weather): the hourly table is kept so degree-days
    can be recomputed at any base temperature."""
    conn = connect(db_path)
    try:
        energy = daily_energy(conn)
        hourly = hourly_weather(conn)
    finally:
        conn.close()
    weather = daily_weather(hourly)
    daily = weather.join(energy, how="left")
    daily["weekend"] = (daily.index.dayofweek >= 5).astype(int)
    daily["month"] = daily.index.to_period("M").astype(str)
    return daily, hourly


def heating_season(daily, months=(10, 11, 12, 1, 2, 3, 4)):
    """Days in the heating season (October to April by default)."""
    return daily[daily.index.month.isin(months)]


def coverage(daily):
    """Complete days per month, for checking what the model can learn from."""
    return daily.groupby("month").agg(
        days=("temp_mean_c", "size"),
        gas_days=("gas_kwh", "count"),
        elec_days=("elec_kwh", "count"),
        temp_mean_c=("temp_mean_c", "mean"),
    ).round(1)


def demo_daily(days=240, seed=3):
    """A made-up house for tests and for running the notebook without real
    data: base load 6 kWh/day (hot water), 5.2 kWh per degree-day below a
    15.5 C balance point, more on windy days, less on sunny ones."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-10-01", periods=days, freq="D", tz=None)
    season = np.cos((idx.dayofyear - 20) / 365.25 * 2 * np.pi)
    temp = 9.5 - 6.0 * season + rng.normal(0, 2.2, days)
    wind = np.clip(rng.gamma(3.0, 1.5, days), 0.5, None)
    solar = np.clip(1.6 + 1.4 * -season + rng.normal(0, 0.5, days), 0.1, None)
    hdd = np.clip(15.5 - temp, 0, None)
    gas = 6.0 + 5.2 * hdd + 0.25 * hdd * (wind - 4.5) - 1.5 * solar + rng.normal(0, 3.0, days)
    gas = np.clip(gas, 0.5, None)
    daily = pd.DataFrame({
        "temp_mean_c": temp, "wind_mean_ms": wind, "solar_kwh_m2": solar,
        "gas_kwh": gas, "elec_kwh": 9 + rng.normal(0, 1.5, days),
    }, index=pd.DatetimeIndex(idx, name="day"))
    # Whole days missing, like the real meter data.
    daily.loc[rng.random(days) < 0.3, ["gas_kwh", "elec_kwh"]] = np.nan
    daily["weekend"] = (daily.index.dayofweek >= 5).astype(int)
    daily["month"] = daily.index.to_period("M").astype(str)
    # Synthetic hourly temperatures for degree-day calculations.
    hours = pd.date_range(idx[0], periods=days * 24, freq="h")
    hourly_t = np.repeat(temp, 24) + 3.0 * np.sin((hours.hour - 9) / 24 * 2 * np.pi)
    hourly = pd.DataFrame({"temp_c": hourly_t}, index=hours)
    return daily, hourly
