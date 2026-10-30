# Glossary

Terms used in the analysis notebooks and results pages, in plain English.

## Heating and buildings

| Term | Meaning |
|---|---|
| **Balance temperature** | The outdoor temperature below which the house needs heating. It's lower than the thermostat setting because the house gets heat for free from people (about 100 W each), cooking, appliances, lights and sunshine. In the energy-signature chart it's where the "hockey stick" bends. |
| **Base load / hot water** | Gas used whatever the weather. With a combi boiler that's mostly hot water (showers, baths, taps), plus the hob if it's gas. It's the flat part of the hockey stick. It's measured from warm days with the heating off, and is what a **normal day at home** uses: it can be zero when everyone's away. |
| **Away day** | A day with nobody home. It's left out of training, because the model predicts a normal day at home. Days with almost no gas (under 0.5 kWh, so no hot water) are caught automatically; other trips can be listed in `analysis/data/away_days.txt`. |
| **Degree-day (heating degree-day, HDD)** | A measure of how cold a day was, relative to the balance temperature. One degree below it for a whole day is one degree-day; 5 °C below for 12 hours is 2.5. Here it's added up hour by hour, so a cold night counts even on a mild-looking day. Heating need is roughly proportional to degree-days. |
| **Energy signature** | How much energy a building uses for the weather, usually shown as daily energy against outdoor temperature. It works like a fingerprint of the building and the way it's heated. |
| **Heat-loss coefficient (W/K)** | How many watts of heat the house loses for each degree of difference between inside and outside. Lower means better insulated. Here it's estimated from gas use, assuming the boiler turns 88% of the gas into heat, so it's approximate. It's best for comparing before and after a change. |
| **kWh (kilowatt-hour)** | A unit of energy: 1 kW for 1 hour. Gas and electricity are both measured in kWh. |
| **kWh/m² (sunshine)** | How much solar energy fell on each square metre of ground during the day. A dull winter day might be 0.5, a bright spring day 5 or more. |
| **Combi boiler** | A boiler that heats radiators and also heats hot water on demand, with no hot-water tank. Gas use therefore includes hot water. |
| **Flow temperature** | How hot the boiler makes the water sent to the radiators. Lower flow temperatures let a condensing boiler run more efficiently, but rooms warm more slowly. |

## Machine learning and statistics

| Term | Meaning |
|---|---|
| **Model** | A formula that turns inputs (here, the weather) into a prediction (here, gas per day). "Fitting" or "training" a model means choosing its numbers to match past data. |
| **Feature** | One input to a model, e.g. degree-days, wind speed or a weekend flag. Choosing good features from raw data is called *feature engineering*. |
| **Interaction (e.g. wind × degree-days)** | A feature made by multiplying two others, used when one thing only matters alongside another. Wind only costs heat when it's cold enough to be heating. |
| **Fixed (constrained) parameter** | A number set from outside knowledge instead of learned. Here hot water is fixed at its measured level, because the data available couldn't pin it down. Using what you already know makes the rest of the model more reliable. |
| **Coefficient / parameter** | A number the model learns, e.g. "4.6 kWh per degree-day". In this model, each one has a physical meaning that can be sanity-checked. |
| **Baseline model** | A deliberately simple model (here, "every day is average") that a real model must beat to be worth anything. |
| **Change-point regression** | A straight-line model that is flat up to a point and sloped after it: here, flat above the balance temperature and sloped below. The change point is found by trying many values and keeping the best. |
| **Grid search** | Trying a setting at regular steps (here, balance temperatures from 10 to 20 °C in 0.25 °C steps) and keeping whichever fits best. |
| **Linear regression (OLS)** | The standard way to fit a straight-line model: pick the numbers that make the squared prediction errors as small as possible. OLS stands for ordinary least squares. |
| **Held-out data / testing** | Data the model wasn't trained on, used to check how well it predicts. Testing on the training data would flatter it. |
| **Leave-one-month-out** | Hold out one month, train on the rest, predict the held-out month; repeat for every month. Whole months are used, not random days, because neighbouring days share the same weather and would give the answer away. |
| **Overfitting** | When a model learns the noise in its training data instead of the real pattern, so it looks good in training but predicts badly. Testing on held-out data catches it. |
| **Residual** | Actual minus predicted for one day. A big positive residual means more gas was used than the weather explains; a big negative one means less. |
| **MAE (mean absolute error)** | The typical size of a miss, in the same units as the prediction (kWh per day). |
| **RMSE (root mean square error)** | Like MAE, but big misses count extra. |
| **CV(RMSE)** | RMSE as a percentage of the average day's gas: the "typical miss in %". One of the two measures ASHRAE Guideline 14 uses to judge building energy models. |
| **NMBE (normalised mean bias error)** | Whether a model systematically over- or under-predicts, as a % of the average. Close to 0 means no overall bias. ASHRAE Guideline 14's other measure. |
| **Confidence interval (95%)** | The range the true value probably lies in, given the data. A wide interval means the data doesn't pin the number down well. |
| **p-value** | Roughly, how likely an effect this big would be to show up by chance if it didn't really exist. Very small (e.g. below 0.01) means the effect is very unlikely to be chance. |
| **HAC standard errors** | A way of calculating confidence intervals that allows for consecutive days being alike (a cold spell lasts days). Without it, the intervals come out too narrow. HAC stands for heteroskedasticity- and autocorrelation-consistent, also known as Newey–West. |

## The data

| Term | Meaning |
|---|---|
| **Complete day** | A day with all 48 half-hours of smart-meter data. Only complete days are used; missing data is never guessed. |
| **UTC** | Coordinated Universal Time. All timestamps are stored in UTC, which is UK time in winter and one hour behind it in summer. |
| **Heating season** | October to April. Summer days (hot water only) are left out of the heating model. |
| **Bright** | Hildebrand Glow's free app and service, which reads the smart meter through the national smart-meter network (the DCC). It's the source of the gas and electricity data. |
| **Open-Meteo** | The free weather service used for hourly temperature, wind, sunshine and rain, back to September 2025. |
