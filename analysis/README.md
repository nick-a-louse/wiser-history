# Analysis (TECH-98)

Machine-learning notebooks on the collector's database. They run on a laptop
(or anywhere with Python), never inside the containers, and read a **copy** of
`wiser.db` opened read-only.

| Notebook | What it answers |
|---|---|
| `01_energy_signature.ipynb` | How much gas the whole house burns for the weather: base load (hot water), heat-loss slope, balance temperature, wind and sun effects, tested month by month. Also the yardstick for the blinds and flow-temperature experiments. |

The code behind the notebooks is in `wiser_ml/` (`data.py` loads tidy daily tables,
`signature.py` holds the models and the testing), and is covered by
`tests/test_analysis.py`.

## Running it

```bash
pip install -r analysis/requirements.txt
mkdir -p analysis/data
# copy the database from the server (the daily backup is a gzipped snapshot)
scp you@your-server:/opt/appdata/wiser/published/backup/wiser.db.gz analysis/data/
gunzip analysis/data/wiser.db.gz
cd analysis && jupyter lab
```

Without `analysis/data/wiser.db` the notebooks run on a **made-up house**
(`wiser_ml.data.demo_daily`), so they can be tried anywhere.

## Privacy

The database shows when the house is empty. `analysis/data/` and `analysis/output/`
are gitignored, and notebooks are committed **without outputs** (a test enforces
it): clear outputs before committing (`jupyter nbconvert --clear-output --inplace *.ipynb`).
Results are shared outside the repo.
