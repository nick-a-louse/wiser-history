"""Half-hourly gas and electricity from the smart meter, via Hildebrand Glow.

The supplier (Fuse Energy) has no public API, but Hildebrand's free Bright
service reads any DCC-connected smart meter (SMETS2, and SMETS1 meters moved
onto the DCC) and exposes it through the Glowmarkt API. Gas is what matters
here: it turns "boiler on-hours" into kWh, so heating changes can be measured
in energy and pounds. The boiler is a combi, so gas also covers hot water.

API facts checked against the live account on 2026-10-22:
* gas.consumption and electricity.consumption, both in kWh (plus .cost
  series in pence, not stored).
* A 10-day half-hourly request works; 31 days is refused (HTTP 400).
* History is available for up to ~13 months.

Like the other clients this uses only the standard library. Nothing here logs
the password, the token, a URL or a resource id.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wiser.glow")

BASE_URL = "https://api.glowmarkt.com/api/v0-1/"
# Bright's public application id: every Glowmarkt client sends this header.
APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

CHUNK_DAYS = 10            # the most the API accepts for half-hourly data
REFRESH_DAYS = 3           # recent days re-read each sync: the DCC can deliver late
EMPTY_CHUNKS_TO_STOP = 2   # back-fill stops after this many empty chunks in a row


class GlowError(RuntimeError):
    pass


def _fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _floor_half_hour(dt):
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def _parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Glow:
    def __init__(self, username, password, timeout=30):
        self._username = username
        self._password = password
        self._timeout = timeout

    def _call(self, path, token=None, body=None, params=None):
        headers = {"Content-Type": "application/json", "applicationId": APPLICATION_ID}
        if token:
            headers["token"] = token
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers=headers, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise GlowError("glow %s: HTTP %s" % (path.split("/")[0], exc.code)) from None
        except Exception as exc:
            raise GlowError("glow request failed: %s" % type(exc).__name__) from None

    def login(self):
        body = self._call("auth", body={"username": self._username, "password": self._password})
        if not body.get("valid") or not body.get("token"):
            raise GlowError("glow login refused")
        return body["token"]

    def resources(self, token):
        """{classifier: resourceId} across every virtual entity on the account."""
        found = {}
        for ve in self._call("virtualentity", token) or []:
            body = self._call("virtualentity/%s/resources" % ve["veId"], token)
            for res in body.get("resources", []):
                found[res.get("classifier")] = res["resourceId"]
        return found

    def catchup(self, token, resource_id):
        """Ask Glowmarkt to fetch any new readings from the DCC. Best effort:
        a failure here doesn't stop the readings being read."""
        try:
            self._call("resource/%s/catchup" % resource_id, token)
        except GlowError as exc:
            log.debug("glow catchup failed (ignored): %s", exc)

    def readings(self, token, resource_id, start, end):
        """Half-hourly readings for [start, end] (UTC datetimes, at most
        CHUNK_DAYS apart). Returns (units, [(ts_utc, value or None), ...]).
        nulls=1 so a missing half-hour comes back as None, not as 0."""
        body = self._call("resource/%s/readings" % resource_id, token, params={
            "from": _fmt(start), "to": _fmt(end), "period": "PT30M",
            "offset": 0, "function": "sum", "nulls": 1,
        })
        rows = []
        for epoch, value in body.get("data") or []:
            ts = datetime.fromtimestamp(epoch, timezone.utc)
            rows.append((ts.strftime("%Y-%m-%dT%H:%M:%SZ"), value))
        return body.get("units"), rows


def sync(store, client, classifiers, history_days, now):
    """Bring energy_halfhourly up to date. Returns {classifier: rows written}.
    Raises GlowError if the account can't be read at all.

    Per classifier:
    1. Nothing stored yet: read the last CHUNK_DAYS only. Until Bright has any
       data that's one cheap request, not 40 a time.
    2. Refresh: re-read from REFRESH_DAYS before the newest stored reading up
       to now, because the DCC can deliver late or correct readings.
    3. Back-fill: walk backwards from the oldest stored reading, a chunk at a
       time, to `history_days` ago. Stops after EMPTY_CHUNKS_TO_STOP empty
       chunks (the start of the meter's history, or what Bright has fetched
       so far); the next sync tries again from there.
    """
    fetched = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = _floor_half_hour(now)
    window_start = _floor_half_hour(now - timedelta(days=history_days))
    token = client.login()
    available = client.resources(token)
    wanted = [c for c in classifiers if c in available]
    if not wanted:
        raise GlowError("glow account has none of: %s" % ", ".join(classifiers))
    for c in classifiers:
        if c not in available:
            log.warning("glow account has no %s resource; skipping it", c)

    written = {}
    for classifier in wanted:
        rid = available[classifier]
        client.catchup(token, rid)
        count = 0

        def fetch(lo, hi):
            units, rows = client.readings(token, rid, lo, hi)
            return store.record_energy(classifier, rows, units, fetched)

        first, last = store.energy_range(classifier)
        if last is None:
            count += fetch(max(now - timedelta(days=CHUNK_DAYS), window_start), now)
        else:
            lo = max(_parse(last) - timedelta(days=REFRESH_DAYS), window_start)
            while lo < now:
                hi = min(lo + timedelta(days=CHUNK_DAYS), now)
                count += fetch(lo, hi)
                lo = hi
        store.commit()

        first, last = store.energy_range(classifier)
        if first is not None:
            cursor, empties = _parse(first), 0
            while cursor > window_start and empties < EMPTY_CHUNKS_TO_STOP:
                lo = max(cursor - timedelta(days=CHUNK_DAYS), window_start)
                n = fetch(lo, cursor)
                count += n
                empties = 0 if n else empties + 1
                cursor = lo
                store.commit()
        written[classifier] = count

    log.info("glow ok: %s", ", ".join("%s +%d" % (c, n) for c, n in written.items()))
    return written


def latest_readings(store, classifiers):
    """{classifier: unix time of the newest stored half-hour} for /metrics."""
    out = {}
    for c in classifiers:
        _, last = store.energy_range(c)
        out[c] = _parse(last).timestamp() if last else 0.0
    return out
