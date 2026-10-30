"""Whole-house "energy signature": daily gas use explained by the weather.

The physics: below some outdoor temperature (the balance point) the house
needs heat, roughly in proportion to how far below it is. Above it, gas only
heats water (a combi boiler). Wind adds heat loss through draughts; sun adds
free heat through the windows. So:

    gas_kwh = base_load + slope * HDD(balance) [+ wind and sun terms]

where HDD(balance) is heating degree-days at the balance temperature. The
balance temperature isn't known in advance; it's found by trying a range and
keeping the one that fits best ("change-point" regression, standard for
building energy analysis).

Four models, simplest first, so each extra piece has to earn its place:
    mean          predicts the average day (the baseline to beat)
    hdd_15_5      degree-days at the UK's conventional 15.5 C base
    changepoint   degree-days at the best-fitting balance temperature
    full          changepoint + wind x degree-days + sun + weekend
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm

from .data import degree_days

BALANCE_GRID = np.arange(10.0, 20.01, 0.25)
MODELS = ("mean", "hdd_15_5", "changepoint", "full")


def design(daily, hourly, base, model):
    """The model's input table (with an intercept column 'const')."""
    hdd = degree_days(hourly["temp_c"], base).reindex(daily.index)
    X = pd.DataFrame({"const": 1.0}, index=daily.index)
    if model == "mean":
        return X
    X["hdd"] = hdd
    if model == "full":
        # Wind only matters when it's cold enough to be heating: draughts carry
        # heat out in proportion to the inside-outside difference.
        X["hdd_x_wind"] = hdd * (daily["wind_mean_ms"] - 4.0)
        X["solar_kwh_m2"] = daily["solar_kwh_m2"]
        X["weekend"] = daily["weekend"]
    return X


@dataclass
class Fit:
    model: str
    base_c: float
    result: object              # statsmodels results
    sse_by_base: dict = field(default_factory=dict)

    def predict(self, daily, hourly):
        """Predicted gas per day, never below the fitted base load. The linear
        terms (above all the sunshine one) can otherwise push a mild, sunny
        day below zero, but sun can only offset heating, never hot water.
        On the real data (2026-10-30) the floor removed two negative
        predictions and cut the held-out error slightly; scaling the sun
        term by heating need instead was physically worse (base load < 0)."""
        X = design(daily, hourly, self.base_c, self.model)
        raw = self.result.predict(X[self.result.model.exog_names])
        return raw.clip(lower=max(float(self.params["const"]), 0.0))

    @property
    def params(self):
        return self.result.params


def _ols(y, X, robust):
    # Daily energy is autocorrelated (a cold spell lasts days), which makes
    # ordinary standard errors too confident. HAC (Newey-West) errors allow for it.
    model = sm.OLS(y, X, missing="drop")
    return model.fit(cov_type="HAC", cov_kwds={"maxlags": 3}) if robust else model.fit()


def fit(daily, hourly, model, robust=True):
    """Fit one model on the days that have gas data."""
    if model not in MODELS:
        raise ValueError("unknown model %r" % model)
    days = daily[daily["gas_kwh"].notna()]
    y = days["gas_kwh"]
    if model in ("mean", "hdd_15_5"):
        base = 15.5
        return Fit(model, base, _ols(y, design(days, hourly, base, model), robust))
    sse = {}
    for base in BALANCE_GRID:
        X = design(days, hourly, base, model).dropna()
        sse[float(base)] = float(sm.OLS(y.loc[X.index], X).fit().ssr)
    best = min(sse, key=sse.get)
    return Fit(model, best, _ols(y, design(days, hourly, best, model), robust), sse)


def metrics(actual, predicted):
    """MAE and RMSE in kWh/day; CV(RMSE) and NMBE as % of the average day.
    CV(RMSE) and NMBE are the measures ASHRAE Guideline 14 uses to judge
    building energy models."""
    err = (predicted - actual).dropna()
    actual = actual.loc[err.index]
    mean = actual.mean()
    rmse = float(np.sqrt((err ** 2).mean()))
    return {
        "days": int(len(err)),
        "mae_kwh": float(err.abs().mean()),
        "rmse_kwh": rmse,
        "cv_rmse_pct": 100 * rmse / mean if mean else float("nan"),
        "nmbe_pct": 100 * float(err.sum()) / (len(err) * mean) if mean else float("nan"),
    }


def leave_one_month_out(daily, hourly, model, min_days=5):
    """Hold out each month in turn, fit on the rest, predict the held-out
    month. Random splits would leak: neighbouring days are near-copies of each
    other (same weather spell), so the model would be tested on what it has
    effectively already seen."""
    days = daily[daily["gas_kwh"].notna()]
    months = [m for m, n in days.groupby("month").size().items() if n >= min_days]
    preds = []
    for m in months:
        train, test = days[days["month"] != m], days[days["month"] == m]
        f = fit(train, hourly, model, robust=False)
        preds.append(f.predict(test, hourly).rename("predicted").to_frame().assign(month=m))
    pred = pd.concat(preds)
    pred["actual"] = days.loc[pred.index, "gas_kwh"]
    by_month = pd.DataFrame({m: metrics(g["actual"], g["predicted"]) for m, g in pred.groupby("month")}).T
    return metrics(pred["actual"], pred["predicted"]), by_month, pred


def compare(daily, hourly, models=MODELS):
    """Leave-one-month-out scores for each model, side by side."""
    rows = {}
    for m in models:
        overall, _, _ = leave_one_month_out(daily, hourly, m)
        rows[m] = overall
    return pd.DataFrame(rows).T


def heat_loss_w_per_k(slope_kwh_per_degree_day, boiler_efficiency=0.88):
    """Whole-house heat-loss coefficient in watts per degree of inside-outside
    difference. Gas kWh x efficiency = heat delivered; per degree-day, spread
    over 24 hours. Ignores heat from the electric heaters, people and
    appliances (the balance temperature absorbs their average)."""
    return slope_kwh_per_degree_day * boiler_efficiency * 1000.0 / 24.0
