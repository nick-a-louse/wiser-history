"""The collector's store and publish step produce what the website reads.
Uses the demo-data generator, so no hub, weather API or S3 is needed."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import demo_data  # noqa: E402


def test_publish_writes_every_tier(tmp_path):
    written = demo_data.build(str(tmp_path), days=12)
    assert "data/meta.json" in written and "data/daily/all.json" in written
    meta = json.loads((tmp_path / "published/data/meta.json").read_text())
    assert len(meta["rooms"]) == 10 and meta["last_success_utc"]
    assert meta["poll_seconds"] == 300 and meta["weather_seconds"] == 900
    assert len(meta["gaps"]) == 1                       # the demo outage is reported, not hidden
    raw = json.loads((tmp_path / ("published/data/raw/%s.json" % meta["months"][-1])).read_text())
    assert {"rooms", "weather", "boiler"} <= set(raw)
    daily = json.loads((tmp_path / "published/data/daily/all.json").read_text())
    r = daily["rooms"][0]
    assert {"day", "room_id", "min_c", "max_c", "mean_c"} <= set(r)   # the room table's "widest day" needs min/max


def test_build_info_metric():
    from collector import main
    assert main.VERSION.startswith(main.RELEASE)
