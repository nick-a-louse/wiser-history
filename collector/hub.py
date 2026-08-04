"""Drayton Wiser hub client.

Talks to the hub's local /data/domain/ endpoint directly. No third-party library:
the original project died when `wiserHeatingAPI` was abandoned, and this is a
single JSON GET with one header.

Field names here were verified against a live hub on 2026-08-04.
"""
import json
import urllib.request

# The hub reports "off" as -20.0C and unknown as -32768, both in tenths.
_SENTINELS = (-200, -32768)


def tenths(value):
    """Convert the hub's tenths-of-a-degree integer to float degrees.

    Returns None for the sentinel values, so "off" and "unknown" never enter
    the data as if they were real readings of -20C.
    """
    if value is None or value in _SENTINELS:
        return None
    return value / 10.0


class HubError(RuntimeError):
    pass


class Hub:
    def __init__(self, ip, secret, timeout=20):
        self._url = "http://%s/data/domain/" % ip
        self._secret = secret
        self._timeout = timeout

    def fetch(self):
        req = urllib.request.Request(self._url, headers={"SECRET": self._secret})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.load(resp)
        except Exception as exc:
            # Never include the secret in the message.
            raise HubError("hub request failed: %s" % exc) from None


def parse(raw):
    """Turn a raw hub response into the structures we store.

    Returns (system, rooms, devices) where rooms/devices are lists of dicts
    matching the SQLite column names.
    """
    roomstats = {r["id"]: r for r in raw.get("RoomStat", [])}
    valves = {v["id"]: v for v in raw.get("SmartValve", [])}
    devices_raw = {d["id"]: d for d in raw.get("Device", [])}

    channels = raw.get("HeatingChannel", []) or [{}]
    channel = channels[0]
    sysd = raw.get("System", {})

    system = {
        "hub_unix_time": sysd.get("UnixTime"),
        "boiler_relay": channel.get("HeatingRelayState"),
        "boiler_demand": channel.get("PercentageDemand"),
        "system_mode": sysd.get("SystemMode"),
        "away_mode": 1 if sysd.get("OverrideType") == "Away" else 0,
    }

    rooms, device_rows = [], []
    device_to_room = {}

    for room in raw.get("Room", []):
        room_id = room.get("id")
        # Singular. `RoomStatIds` does not exist - see the requirements doc.
        stat_id = room.get("RoomStatId")
        valve_ids = room.get("SmartValveIds") or []

        stat = roomstats.get(stat_id) if stat_id is not None else None

        rooms.append({
            "room_id": room_id,
            "name": room.get("Name"),
            "temp_c": tenths(room.get("CalculatedTemperature")),
            # The hub already prefers the RoomStat where one exists (verified);
            # recording which source was in play keeps that reversible.
            "temp_source": "roomstat" if stat else ("trv" if valve_ids else "none"),
            "setpoint_c": tenths(room.get("CurrentSetPoint")),
            "scheduled_setpoint_c": tenths(room.get("ScheduledSetPoint")),
            "demand_pct": room.get("PercentageDemand"),
            "output_state": room.get("ControlOutputState"),
            "mode": room.get("Mode"),
            # Not every room carries these keys - absence is normal, not an error.
            "effective_mode": room.get("EffectiveMode"),
            "setpoint_origin": room.get("SetpointOrigin"),
            "window_state": room.get("WindowState"),
            "humidity_pct": stat.get("MeasuredHumidity") if stat else None,
            "heating_rate": room.get("HeatingRate"),
            "comfort_score": room.get("ComfortModeScore"),
        })

        if stat_id is not None:
            device_to_room[stat_id] = room_id
        for vid in valve_ids:
            device_to_room[vid] = room_id

    for dev_id, dev in devices_raw.items():
        stat = roomstats.get(dev_id)
        valve = valves.get(dev_id)
        source = stat or valve or {}
        voltage = dev.get("BatteryVoltage")
        device_rows.append({
            "device_id": dev_id,
            "room_id": device_to_room.get(dev_id),
            "product_type": dev.get("ProductType"),
            "temp_c": tenths(source.get("MeasuredTemperature")),
            "humidity_pct": stat.get("MeasuredHumidity") if stat else None,
            "setpoint_c": tenths(source.get("SetPoint")),
            "demand_pct": source.get("PercentageDemand"),
            "battery_level": dev.get("BatteryLevel"),
            # Reported in tenths of a volt (29 -> 2.9V).
            "battery_voltage": voltage / 10.0 if voltage is not None else None,
            "signal": dev.get("DisplayedSignalStrength"),
        })

    return system, rooms, device_rows


def geo_position(raw):
    """The hub knows its own coordinates, so no geocoding service is needed."""
    geo = raw.get("System", {}).get("GeoPosition") or {}
    lat, lon = geo.get("Latitude"), geo.get("Longitude")
    if lat is None or lon is None:
        raise HubError("hub did not report a GeoPosition")
    return lat, lon
