"""Write the results page (Markdown + charts) for GitHub.

Includes real dates (Nick, 2026-10-30: the data is historical, so dated
results are fine in the repo). Day-by-day readings themselves are not
published, only model results, the most surprising days, and charts.
"""
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import data, plots, signature as sg  # noqa: E402

MODEL_LABELS = {
    "mean": "Every day is average (baseline)",
    "hdd_15_5": "Degree-days at 15.5 °C",
    "changepoint": "Degree-days at a learned balance temperature",
    "full": "Plus wind, sunshine and weekends",
}


def _month_name(period):
    return pd.Period(period).strftime("%b %Y")


def _save(fig, path):
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _charts(daily, hourly, full, pred, img_dir):
    plots.style()
    d = data.home_days(daily).copy()
    d["season"] = plots.season_of(d.index)
    fig, ax = plt.subplots()
    for season, colour in plots.SEASON_COLOURS.items():
        s = d[d.season == season]
        if len(s):
            ax.scatter(s.temp_mean_c, s.gas_kwh, s=30, color=colour, alpha=0.85,
                       edgecolor=plots.SURFACE, linewidth=0.8, label="%s (%d days)" % (season, len(s)))
    ax.axvline(full.base_c, color=plots.MUTED, linewidth=1, linestyle="--")
    ax.annotate("balance %.1f °C" % full.base_c, (full.base_c, ax.get_ylim()[1]),
                textcoords="offset points", xytext=(-4, -14), ha="right", color=plots.INK)
    ax.set(title="Daily gas use against outdoor temperature",
           xlabel="Daily mean outdoor temperature (°C)", ylabel="Gas used (kWh per day)")
    ax.legend(loc="lower left")       # the top right holds the balance-line label
    _save(fig, img_dir / "signature.png")

    sse = pd.Series(full.sse_by_base)
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(sse.index, sse.values / sse.min(), color=plots.BLUE)
    ax.axvline(full.base_c, color=plots.MUTED, linewidth=1, linestyle="--")
    ax.set(title="Choosing the balance temperature (lower is a better fit)",
           xlabel="Balance temperature tried (°C)", ylabel="Error relative to best")
    _save(fig, img_dir / "balance.png")

    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.axhline(0, color=plots.MUTED, linewidth=1)
    resid = pred.actual - pred.predicted
    ax.scatter(pred.index, resid, s=24, color=plots.BLUE, edgecolor=plots.SURFACE, linewidth=0.6)
    ax.set(title="How far off each day's prediction was (held-out months)",
           ylabel="Actual minus predicted (kWh)", xlabel="")
    _save(fig, img_dir / "residuals.png")

    fig, ax = plt.subplots(figsize=(6.4, 6.0))
    top = max(pred.actual.max(), pred.predicted.max()) * 1.05
    ax.plot([0, top], [0, top], color=plots.MUTED, linewidth=1, linestyle="--")
    ax.scatter(pred.predicted, pred.actual, s=26, color=plots.BLUE, edgecolor=plots.SURFACE, linewidth=0.6)
    ax.set(xlim=(0, top), ylim=(0, top), title="Predicted against actual (held-out months)",
           xlabel="Predicted gas (kWh per day)", ylabel="Actual gas (kWh per day)")
    ax.annotate("on the dashed line = perfect", (top * 0.04, top * 0.92), color=plots.MUTED)
    _save(fig, img_dir / "predicted_vs_actual.png")


def write_results_page(daily_all, hourly, out_dir, generated_label, source_label):
    """Fit, test and write results/01_energy_signature.md plus charts."""
    out_dir = pathlib.Path(out_dir)
    img_dir = out_dir / "img" / "01_energy_signature"
    img_dir.mkdir(parents=True, exist_ok=True)

    daily = data.heating_season(daily_all)
    HW = data.hot_water_kwh(daily_all)
    hw = HW["kwh"] if HW else None
    scores = sg.compare(daily, hourly, hot_water=hw)
    full = sg.fit(daily, hourly, "full", hot_water=hw)
    overall, by_month, pred = sg.leave_one_month_out(daily, hourly, "full", hot_water=hw)
    away = daily_all[daily_all["away"].astype(bool) & daily_all["gas_kwh"].notna()]
    _charts(daily, hourly, full, pred, img_dir)

    p, ci = full.params, full.result.conf_int()
    used = data.home_days(daily)
    months = sorted(used["month"].unique())
    cov = data.coverage(daily_all)
    resid_elec = pred.assign(r=pred.actual - pred.predicted).join(daily_all["elec_kwh"]).dropna()
    r_elec = resid_elec[["r", "elec_kwh"]].corr().iloc[0, 1] if len(resid_elec) > 5 else float("nan")

    def row(name, meaning):
        return "| %s | %s | %.2f | %.2f to %.2f |" % (name, meaning, p[name], ci.loc[name, 0], ci.loc[name, 1])

    lines = [
        "# Results: whole-house energy signature",
        "",
        "*Generated %s from %s by `analysis/make_results.py` (TECH-98). "
        "Summary figures only: day-by-day data stays out of the repo. "
        "Terms are explained in the [glossary](../GLOSSARY.md); the method is in "
        "[`01_energy_signature.ipynb`](../01_energy_signature.ipynb).*" % (generated_label, source_label),
        "",
        "**Question:** how much gas should the house burn on a given day, for the weather? "
        "The answer describes the house in a few physical numbers, and is the yardstick for "
        "measuring changes such as new blinds or a lower flow temperature.",
        "",
        "## Data",
        "",
        "- **%d complete heating-season days** (all 48 half-hours of gas), %s to %s. "
        "Days with any missing half-hour are left out, never guessed." % (
            len(used), _month_name(months[0]), _month_name(months[-1])),
        "- **Days away are left out**: the model predicts a normal day at home. %s" % (
            "Left out: " + ", ".join(d.strftime("%a %d %b %Y") for d in away.index) + "."
            if len(away) else "None found."),
        "- **Hot water is measured, not guessed**: %s" % (
            "about **%.1f kWh/day** (typically %.1f to %.1f), from %d warm days with the heating off. "
            "The model's own estimate was poor, so it's fixed at this value." % (HW["kwh"], HW["low"], HW["high"], HW["days"])
            if HW else "not enough warm days yet, so the model estimates it."),
        "- Weather: Open-Meteo hourly temperature, wind and sunshine.",
        "",
        "| Month | Complete gas days | Average outdoor temp (°C) |",
        "|---|---:|---:|",
    ]
    for m, r in cov.iterrows():
        if r["gas_days"]:
            lines.append("| %s | %d | %.1f |" % (_month_name(m), r["gas_days"], r["temp_mean_c"]))
    lines += [
        "",
        "## The energy signature",
        "",
        "![Daily gas against outdoor temperature](img/01_energy_signature/signature.png)",
        "",
        "Colder days use more gas, in a roughly straight line, down to a floor of hot-water use on "
        "warm days. The dashed line is the learned balance temperature.",
        "",
        "## How well the models predict",
        "",
        "Each month was held out in turn, and the model trained on the other months predicted it.",
        "",
        "| Model | Typical miss (MAE, kWh/day) | CV(RMSE) | Bias (NMBE) |",
        "|---|---:|---:|---:|",
    ]
    for m in sg.MODELS:
        s = scores.loc[m]
        lines.append("| %s | %.1f | %.0f%% | %+.1f%% |" % (
            MODEL_LABELS[m], s["mae_kwh"], s["cv_rmse_pct"], s["nmbe_pct"]))
    lines += [
        "",
        "![Predicted against actual](img/01_energy_signature/predicted_vs_actual.png)",
        "",
        "## What the model learned (full model)",
        "",
        "| Term | Meaning | Estimate | 95% confidence interval |",
        "|---|---|---:|---:|",
    ] + ([row("const", "Hot water etc., kWh/day on a weekday")] if "const" in p else
         ["| hot water | Fixed at the measured level, kWh/day | %.2f | (measured) |" % full.base_load]) + [
        row("hdd", "kWh per degree-day of cold"),
        row("hdd_x_wind", "Extra kWh per degree-day per m/s of wind above 4"),
        row("solar_kwh_m2", "kWh per kWh/m² of sunshine"),
        row("weekend", "Extra kWh on a weekend day"),
        "",
        "Balance temperature: **%.2f °C** (best of a grid search from 10 to 20 °C):" % full.base_c,
        "",
        "![Choosing the balance temperature](img/01_energy_signature/balance.png)",
        "",
        "## In plain English",
        "",
        "- Heating is needed below about **%.1f °C** outside (daily average)." % full.base_c,
        "- Each degree-day of cold costs about **%.1f kWh** of gas, a heat-loss coefficient of roughly "
        "**%.0f W/K** (assuming 88%% boiler efficiency; approximate)." % (p["hdd"], sg.heat_loss_w_per_k(p["hdd"])),
        "- Hot water: about **%.1f kWh/day** on a normal day at home; none when away." % full.base_load,
        "- **Wind** adds heat loss when it's cold: 10 degree-days with 5 m/s more wind costs about "
        "**%.0f kWh** extra." % (10 * 5 * p["hdd_x_wind"]),
        "- **Sunshine** saves about **%.1f kWh** of gas per kWh/m²." % -p["solar_kwh_m2"],
        "- **Weekends** use about **%+.0f kWh/day** compared with weekdays in the same weather "
        "(people at home, heating on longer)." % p["weekend"],
        "- Electricity use vs the gas the model can't explain: correlation **%+.2f**, so no clear "
        "sign at whole-house level of the electric heaters standing in for gas." % r_elec,
        "",
        "## Month by month (held out)",
        "",
        "| Month | Days | Typical miss (kWh/day) | Bias (NMBE) |",
        "|---|---:|---:|---:|",
    ]
    for m, r in by_month.iterrows():
        lines.append("| %s | %d | %.1f | %+.0f%% |" % (_month_name(m), r["days"], r["mae_kwh"], r["nmbe_pct"]))
    lines += [
        "",
        "Positive bias means the model predicted **more** gas than was used.",
        "",
        "## The days the model got most wrong",
        "",
        "![Residuals over time](img/01_energy_signature/residuals.png)",
        "",
    ]
    surprising = pred.assign(residual=pred.actual - pred.predicted).join(
        daily_all[["temp_mean_c", "wind_mean_ms", "solar_kwh_m2", "weekend"]])
    for title, part in (("More gas than the weather explains", surprising.nlargest(6, "residual")),
                        ("Less gas than the weather explains", surprising.nsmallest(6, "residual"))):
        lines += ["**%s**" % title, "",
                  "| Day | Actual (kWh) | Predicted (kWh) | Difference | Outside (°C) | Wind (m/s) | Sun (kWh/m²) |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for day, r in part.iterrows():
            lines.append("| %s | %.1f | %.1f | %+.1f | %.1f | %.1f | %.1f |" % (
                day.strftime("%a %d %b %Y"), r.actual, r.predicted, r.residual,
                r.temp_mean_c, r.wind_mean_ms, r.solar_kwh_m2))
        lines.append("")
    lines += [
        "## Caveats",
        "",
        "- Whole house only; room-by-room heat loss is the next step (needs the floor plan).",
        "- Gas includes hot water (combi boiler), which adds day-to-day noise the weather can't explain.",
        "- The start of each heating season is fuzzy: in early October the heating may not be on yet.",
        "- The heat-loss coefficient is for before/after comparisons, not for comparing with other houses.",
        "- Predictions never go below the hot-water level (sun can offset heating, not hot water).",
        "- The balance temperature and the hot-water level trade off: fixing hot water at the measured, lower "
        "level moved the balance point up. Treat it as approximate; the slope, wind, sun and weekend effects "
        "were stable either way.",
        "- Days away with some gas still used (e.g. frost protection) need listing in "
        "`analysis/data/away_days.txt`, or they look like unusually efficient days.",
        "",
    ]
    page = out_dir / "01_energy_signature.md"
    page.write_text("\n".join(lines))
    return page
