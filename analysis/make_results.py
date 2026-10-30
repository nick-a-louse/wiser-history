"""Regenerate the public-safe results pages in results/.

    python make_results.py data/wiser.db          # real data (copy of the server DB);
                                                   # data/away_days.txt is used if present
    python make_results.py                         # made-up house, for trying it out

Only summary figures and undated charts are written (see wiser_ml/report.py).
Commit results/ after checking it.
"""
import os
import sys
from datetime import date

from wiser_ml import data, report

if __name__ == "__main__":
    if len(sys.argv) > 1:
        away = "data/away_days.txt"
        daily, hourly = data.load_daily(sys.argv[1], away if os.path.exists(away) else None)
        source = "the collector's database"
    else:
        daily, hourly = data.demo_daily()
        source = "MADE-UP DEMO DATA"
    page = report.write_results_page(daily, hourly, "results", date.today().strftime("%B %Y"), source)
    print("wrote", page)
