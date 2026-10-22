"""OpenWeatherMap One Call 4.0 client.

WARNING: OWM embeds the API key in the `prev`/`next` URLs it returns. Any code
that logs or stores a raw response writes the credential to disk. Responses are
scrubbed here, at the boundary, so nothing downstream can leak it by accident.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

# One Call 4.0 uses sub-paths; /data/4.0/onecall on its own returns 404.
_CURRENT_URL = "https://api.openweathermap.org/data/4.0/onecall/current"

# Fields whose values contain `appid=<key>`.
_KEY_BEARING = {"prev", "next"}


class WeatherError(RuntimeError):
    pass


def scrub(obj):
    """Strip key-bearing fields from an OWM response, recursively."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in _KEY_BEARING}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


class Weather:
    def __init__(self, api_key, timeout=20):
        self._key = api_key
        self._timeout = timeout

    def current(self, lat, lon):
        query = urllib.parse.urlencode({
            "lat": lat, "lon": lon, "appid": self._key, "units": "metric",
        })
        try:
            with urllib.request.urlopen(
                _CURRENT_URL + "?" + query, timeout=self._timeout
            ) as resp:
                body = scrub(json.load(resp))
        except urllib.error.HTTPError as exc:
            # The status code is safe to log and says what went wrong (401 =
            # key not active or no One Call subscription, 429 = over the cap).
            # Never log exc.url or the body: both can carry the key.
            raise WeatherError("weather request failed: HTTP %s" % exc.code) from None
        except Exception as exc:
            raise WeatherError("weather request failed: %s" % type(exc).__name__) from None

        data = body.get("data") or []
        if not data:
            raise WeatherError("weather response contained no data")
        return _row(data[0])


def _amount(cur, key):
    """Rain or snow in the last hour, mm.

    OWM leaves `rain`/`snow` out entirely when there is none ("did not occur at
    the time of measurement"), so absent means 0, not unknown. Without this
    the dry hours would be NULL and look like missing data.
    """
    value = (cur.get(key) or {}).get("1h")
    return float(value) if value is not None else 0.0


def _row(cur):
    conditions = (cur.get("weather") or [{}])[0]
    return {
        "observed_unix": cur.get("dt"),
        "temp_c": cur.get("temp"),
        "feels_like_c": cur.get("feels_like"),
        "humidity_pct": cur.get("humidity"),
        "dew_point_c": cur.get("dew_point"),
        "pressure_hpa": cur.get("pressure"),
        # Cloud cover drives the solar-gain analysis; wind drives heat loss.
        "clouds_pct": cur.get("clouds"),
        "wind_speed": cur.get("wind_speed"),
        "wind_gust": cur.get("wind_gust"),
        "wind_deg": cur.get("wind_deg"),
        "uvi": cur.get("uvi"),
        "visibility": cur.get("visibility"),
        # Amounts, not just the "Rain"/"Snow" label: data.rain.1h / data.snow.1h, mm/h.
        "rain_1h_mm": _amount(cur, "rain"),
        "snow_1h_mm": _amount(cur, "snow"),
        "condition_id": conditions.get("id"),   # e.g. 500 light rain vs 502 heavy rain
        "condition": conditions.get("main"),
        "description": conditions.get("description"),
        "sunrise_unix": cur.get("sunrise"),
        "sunset_unix": cur.get("sunset"),
    }
