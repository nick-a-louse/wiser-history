"""TECH-98: the analysis package (daily data and the energy-signature models)."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))
pd = pytest.importorskip("pandas")
pytest.importorskip("statsmodels")
from collector.store import Store  # noqa: E402
from wiser_ml import data, signature as sg  # noqa: E402


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_daily_tables_keep_only_complete_days(tmp_path):
    path = str(tmp_path / "w.db")
    store = Store(path)
    day0 = datetime(2026, 1, 10, tzinfo=timezone.utc)
    hours = []
    for d in range(3):
        n = 20 if d == 2 else 24                         # third day: weather incomplete
        for h in range(n):
            t = day0 + timedelta(days=d, hours=h)
            hours.append({"ts_utc": iso(t), "temp_c": 5.0, "wind_speed": 4.0,
                          "shortwave_wm2": 100.0 if 8 <= h < 16 else 0.0, "rain_mm": 0.0})
    store.record_weather_hourly(hours, "archive", "t")
    for d, n in ((0, 48), (1, 47)):                      # second day: one half-hour missing
        rows = [(iso(day0 + timedelta(days=d, minutes=30 * i)), 1.0) for i in range(n)]
        store.record_energy("gas.consumption", rows, "kWh", "t")
    store.commit()
    store.close()

    daily, hourly = data.load_daily(path)
    assert list(daily.index.strftime("%Y-%m-%d")) == ["2026-01-10", "2026-01-11"]   # day 3 dropped
    assert daily.loc["2026-01-10", "gas_kwh"] == 48.0
    assert np.isnan(daily.loc["2026-01-11", "gas_kwh"])                            # never guessed
    assert daily.loc["2026-01-10", "solar_kwh_m2"] == pytest.approx(0.8)            # 8 h x 100 W/m2
    assert daily.loc["2026-01-10", "weekend"] == 1                                   # a Saturday


def test_degree_days_are_summed_hour_by_hour():
    idx = pd.date_range("2026-01-01", periods=24, freq="h")
    temps = pd.Series([10.0] * 12 + [20.0] * 12, index=idx)       # mean 15 C
    dd = data.degree_days(temps, 15.5)
    assert dd.iloc[0] == pytest.approx(12 * 5.5 / 24)             # only the cold hours count


@pytest.fixture(scope="module")
def demo():
    return data.demo_daily()


def test_full_model_recovers_the_made_up_house(demo):
    daily, hourly = demo
    f = sg.fit(daily, hourly, "full")
    p = f.params
    assert abs(f.base_c - 15.5) <= 0.75
    assert p["hdd"] == pytest.approx(5.2, abs=0.4)
    assert p["hdd_x_wind"] == pytest.approx(0.25, abs=0.08)
    assert p["solar_kwh_m2"] == pytest.approx(-1.5, abs=0.6)


def test_each_model_must_beat_the_one_before(demo):
    daily, hourly = demo
    scores = sg.compare(daily, hourly)
    assert scores.loc["hdd_15_5", "mae_kwh"] < 0.5 * scores.loc["mean", "mae_kwh"]
    assert scores.loc["full", "mae_kwh"] < scores.loc["changepoint", "mae_kwh"]


def test_held_out_months_are_never_seen_in_training(demo, monkeypatch):
    daily, hourly = demo
    seen = []
    real = sg.fit

    def spy(train, hourly_, model, robust=True):
        seen.append(set(train["month"]))
        return real(train, hourly_, model, robust)
    monkeypatch.setattr(sg, "fit", spy)
    overall, by_month, pred = sg.leave_one_month_out(daily, hourly, "hdd_15_5")
    for held_out, months_trained in zip(by_month.index, seen):
        assert held_out not in months_trained
    assert overall["days"] == len(pred)


def test_heat_loss_coefficient():
    assert sg.heat_loss_w_per_k(24.0, boiler_efficiency=1.0) == pytest.approx(1000.0)


def test_committed_notebooks_carry_no_outputs():
    """Outputs from the real database would put household data in the repo."""
    folder = os.path.join(HERE, "..", "analysis")
    books = [f for f in os.listdir(folder) if f.endswith(".ipynb")]
    assert books
    for name in books:
        nb = json.load(open(os.path.join(folder, name)))
        for cell in nb["cells"]:
            if cell["cell_type"] == "code":
                assert not cell.get("outputs"), name
                assert cell.get("execution_count") is None, name
