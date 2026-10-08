"""Fetch REAL data for the stand-in satellite (CroCube, NORAD 62394) from SatNOGS.

No account or token is needed. Two things are fetched and cached under data/real/
so the prototype runs offline (and reproducibly) afterwards:

  tle.json     current orbital elements          (db.satnogs.org/api/tle)
  passes.json  recent real ground-station passes (network.satnogs.org/api/observations)
               with every demodulated frame classified, and the housekeeping
               beacons decoded with the published satnogs-decoders Kaitai struct.

Usage:  python tools/fetch_real_data.py [pages] [max_frames_per_pass]
"""
import concurrent.futures as cf
import json
import re
import sys
import urllib.request
from pathlib import Path

from satnogsdecoders.decoder import Crocube, get_fields

NORAD = 62394
OUT = Path(__file__).resolve().parent.parent / "data" / "real"
UA = {"User-Agent": "somaiyasat-cc-usecase/1.0 (student project)"}
BEACON_KEYS = {"uhf": "U", "psu": "PSU", "obc": "OBC", "sol": "SOL", "mgs": "MGS", "atr": "ATR", "vhf": "V"}


def get(url, raw=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        body = r.read()
        return (body, r.headers) if raw else (json.loads(body), r.headers)


def classify(raw):
    """Return (kind, fields). kind is a beacon type, 'message', or 'payload' (file/image chunk)."""
    try:
        fields = get_fields(Crocube.from_bytes(raw))
    except Exception:
        return "payload", None
    fields = {k: v for k, v in fields.items() if "callsign" not in k}
    for prefix, kind in BEACON_KEYS.items():
        if f"{prefix}_beacon_raw" in fields or any(k.startswith(prefix + "_") for k in fields):
            return kind, fields
    return ("message", fields) if "message" in fields else ("other", fields)


def fetch_frame(item):
    obs_id, url = item
    try:
        raw, _ = get(url, raw=True)
    except Exception:
        return obs_id, url, None
    return obs_id, url, raw


def main(pages=6, cap=50):
    OUT.mkdir(parents=True, exist_ok=True)
    tle, _ = get(f"https://db.satnogs.org/api/tle/?format=json&norad_cat_id={NORAD}")
    (OUT / "tle.json").write_text(json.dumps(tle[0], indent=1))
    print("TLE epoch source:", tle[0]["tle_source"], "updated", tle[0]["updated"])

    url = f"https://network.satnogs.org/api/observations/?format=json&status=good&satellite__norad_cat_id={NORAD}"
    observations = []
    for _ in range(pages):
        page, headers = get(url)
        observations += page
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
        if not nxt:
            break
        url = nxt.group(1)

    passes = {
        o["id"]: {
            "id": o["id"], "start": o["start"], "end": o["end"],
            "station": o["station_name"], "lat": o["station_lat"], "lng": o["station_lng"],
            "max_elevation": o["max_altitude"], "frames_total": len(o["demoddata"]),
            "counts": {}, "bytes": {}, "beacons": [],
        }
        for o in observations
    }
    jobs = [(o["id"], d["payload_demod"]) for o in observations for d in o["demoddata"][:cap]]
    print(f"{len(observations)} passes, downloading {len(jobs)} frames")
    with cf.ThreadPoolExecutor(12) as pool:
        for obs_id, furl, raw in pool.map(fetch_frame, jobs):
            if raw is None:
                continue
            kind, fields = classify(raw)
            p = passes[obs_id]
            p["counts"][kind] = p["counts"].get(kind, 0) + 1
            p["bytes"][kind] = p["bytes"].get(kind, 0) + len(raw)
            if fields is not None:
                ts = re.search(r"_(\d{4}-\d\d-\d\dT[\d\-:.]+)", furl)
                p["beacons"].append({"t": ts.group(1) if ts else None, "kind": kind, "fields": fields})

    out = sorted(passes.values(), key=lambda p: p["start"])
    (OUT / "passes.json").write_text(json.dumps(out, indent=1))
    total = {}
    for p in out:
        for k, v in p["counts"].items():
            total[k] = total.get(k, 0) + v
    print("frame mix:", total)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
