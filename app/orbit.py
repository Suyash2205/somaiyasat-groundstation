"""Orbit and link geometry from REAL orbital elements (SGP4).

Everything here is derived from the cached TLE of the stand-in satellite
(data/real/tle.json). Nothing is synthetic except the link-budget constants.
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sgp4.api import Satrec, jday

ROOT = Path(__file__).resolve().parent.parent
SLOT_S = 15                      # one scheduling slot = 15 s of pass time
RE = 6378.137                    # km
F = 1 / 298.257223563
GROUND = {"name": "Vidyavihar, Mumbai", "lat": 19.0728, "lng": 72.8997, "alt_km": 0.014}


def load_tle():
    return json.loads((ROOT / "data" / "real" / "tle.json").read_text())


def satrec(tle=None):
    tle = tle or load_tle()
    return Satrec.twoline2rv(tle["tle1"], tle["tle2"])


def _jd(times):
    jd, fr = [], []
    for t in times:
        a, b = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
        jd.append(a)
        fr.append(b)
    return np.array(jd), np.array(fr)


def _gmst(jd, fr):
    t = (jd - 2451545.0 + fr) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0 + fr) + 0.000387933 * t * t - t ** 3 / 38710000.0
    return np.radians(g % 360.0)


def _sun_eci(jd, fr):
    """Low-precision unit vector to the Sun (good to ~0.01 deg, plenty for eclipse tests)."""
    n = jd - 2451545.0 + fr
    L = np.radians((280.460 + 0.9856474 * n) % 360)
    g = np.radians((357.528 + 0.9856003 * n) % 360)
    lam = L + np.radians(1.915) * np.sin(g) + np.radians(0.020) * np.sin(2 * g)
    eps = np.radians(23.439 - 0.0000004 * n)
    return np.stack([np.cos(lam), np.cos(eps) * np.sin(lam), np.sin(eps) * np.sin(lam)], axis=-1)


def _station_ecef(lat, lng, alt_km):
    la, lo = math.radians(lat), math.radians(lng)
    e2 = F * (2 - F)
    n = RE / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return np.array([(n + alt_km) * math.cos(la) * math.cos(lo),
                     (n + alt_km) * math.cos(la) * math.sin(lo),
                     (n * (1 - e2) + alt_km) * math.sin(la)])


def track(sat, times, lat, lng, alt_km=0.0):
    """Elevation/azimuth/range from a ground point, sub-satellite point and sunlit flag."""
    jd, fr = _jd(times)
    err, r, _ = sat.sgp4_array(jd, fr)
    r = np.asarray(r)
    th = _gmst(jd, fr)
    c, s = np.cos(th), np.sin(th)
    ecef = np.stack([c * r[:, 0] + s * r[:, 1], -s * r[:, 0] + c * r[:, 1], r[:, 2]], axis=-1)
    d = ecef - _station_ecef(lat, lng, alt_km)
    la, lo = math.radians(lat), math.radians(lng)
    east = -math.sin(lo) * d[:, 0] + math.cos(lo) * d[:, 1]
    north = (-math.sin(la) * math.cos(lo) * d[:, 0] - math.sin(la) * math.sin(lo) * d[:, 1]
             + math.cos(la) * d[:, 2])
    up = (math.cos(la) * math.cos(lo) * d[:, 0] + math.cos(la) * math.sin(lo) * d[:, 1]
          + math.sin(la) * d[:, 2])
    rng = np.linalg.norm(d, axis=1)
    elev = np.degrees(np.arcsin(up / rng))
    az = np.degrees(np.arctan2(east, north)) % 360
    rad = np.linalg.norm(ecef, axis=1)
    sun = _sun_eci(jd, fr)
    along = np.einsum("ij,ij->i", r, sun)
    perp = np.linalg.norm(r - along[:, None] * sun, axis=1)
    sunlit = ~((along < 0) & (perp < RE))
    return {
        "elev": elev, "az": az, "range_km": rng, "sunlit": sunlit, "ok": err == 0,
        "sub_lat": np.degrees(np.arcsin(ecef[:, 2] / rad)),
        "sub_lng": np.degrees(np.arctan2(ecef[:, 1], ecef[:, 0])),
        "alt_km": rad - RE,
    }


def find_passes(sat, lat, lng, start, hours=24.0, min_max_elev=3.0, alt_km=0.0):
    """Real passes over a ground point, each sampled once per scheduling slot."""
    step = 20
    n = int(hours * 3600 / step)
    coarse_t = [start + timedelta(seconds=i * step) for i in range(n)]
    up = track(sat, coarse_t, lat, lng, alt_km)["elev"] > 0
    passes, i = [], 0
    while i < n:
        if not up[i]:
            i += 1
            continue
        j = i
        while j < n and up[j]:
            j += 1
        t0 = coarse_t[max(i - 1, 0)]
        t1 = coarse_t[min(j, n - 1)]
        fine_t = [t0 + timedelta(seconds=k) for k in range(int((t1 - t0).total_seconds()) + 1)]
        fe = track(sat, fine_t, lat, lng, alt_km)["elev"]
        vis = np.nonzero(fe > 0)[0]
        if len(vis) > 2 * SLOT_S and fe.max() >= min_max_elev:
            aos, los = fine_t[vis[0]], fine_t[vis[-1]]
            slots = max(2, int((los - aos).total_seconds() // SLOT_S))
            st = [aos + timedelta(seconds=(k + 0.5) * SLOT_S) for k in range(slots)]
            tr = track(sat, st, lat, lng, alt_km)
            passes.append({
                "aos": aos.isoformat().replace("+00:00", "Z"),
                "los": los.isoformat().replace("+00:00", "Z"),
                "max_elev": round(float(fe.max()), 1),
                "elev": [round(float(x), 2) for x in np.maximum(tr["elev"], 0.0)],
                "az": [round(float(x), 1) for x in tr["az"]],
                "range_km": [round(float(x), 1) for x in tr["range_km"]],
                "sunlit": [bool(x) for x in tr["sunlit"]],
            })
        i = j + 1
    return passes


def now_utc():
    return datetime.now(timezone.utc)
