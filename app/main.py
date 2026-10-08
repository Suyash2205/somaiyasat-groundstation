"""SomaiyaSat ground-station service: router API + dashboard.

  GET  /api/health    liveness
  GET  /api/config    ground station, link constants, orbital elements, model info
  GET  /api/passes    REAL upcoming passes over the ground station (SGP4)
  GET  /api/real      REAL decoded telemetry and station list from SatNOGS (cached)
  GET  /api/metrics   offline evaluation of the trained router
  POST /api/decide    one routing decision for one telemetry sample
  POST /api/plan      play a whole pass with the AI router and the fixed-priority baseline
"""
import json
import os
import time
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import orbit, router

app = FastAPI(title="SomaiyaSat ground station", version="1.0")
MODEL = router.TreeModel()
STARTED = time.time()


class Telemetry(BaseModel):
    snr_db: float = Field(ge=-30, le=40)
    elev_deg: float = Field(ge=0, le=90)
    rising: bool
    max_elev_deg: float = Field(ge=0, le=90)
    slots_left: int = Field(ge=1, le=80)
    soc_pct: float = Field(ge=0, le=100, description="battery state of charge")
    sunlit: bool
    hk_queued: int = Field(ge=0, le=router.H_MAX)
    img_queued: int = Field(ge=0, le=router.I_MAX)
    voice_queued: int = Field(ge=0, le=router.V_MAX)
    safe_mode: bool = False


class PassIn(BaseModel):
    elev: list[float] = Field(min_length=2, max_length=80)
    range_km: list[float] = Field(min_length=2, max_length=80)
    sunlit: list[bool] = Field(min_length=2, max_length=80)

    @model_validator(mode="after")
    def check(self):
        if not (len(self.elev) == len(self.range_km) == len(self.sunlit)):
            raise ValueError("elev, range_km and sunlit must have one value per slot")
        if any(not 0 <= e <= 90 for e in self.elev) or any(not 150 <= r <= 5000 for r in self.range_km):
            raise ValueError("elevation must be 0-90 deg and range 150-5000 km")
        return self


class Start(BaseModel):
    soc: float = Field(ge=router.FLOOR_PCT, le=100)
    hk: int = Field(ge=0, le=6)
    img: int = Field(ge=0, le=router.I_MAX)
    voice: int = Field(ge=0, le=router.V_MAX)


class Events(BaseModel):
    """Per-slot operator/fault schedule. Every list, if given, has one entry per slot."""
    extra_loss_db: Optional[list[float]] = None
    eclipse: Optional[list[bool]] = None
    safe_mode: Optional[list[bool]] = None
    corrupt: Optional[list[bool]] = None
    model_fault: Optional[list[bool]] = None
    soc_set: Optional[list[Optional[float]]] = None


class PlanIn(BaseModel):
    pass_: PassIn = Field(alias="pass")
    start: Start
    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    events: Events = Events()


def _state(t: Telemetry):
    return {"snr": t.snr_db, "elev": t.elev_deg, "rising": t.rising, "max_elev": t.max_elev_deg,
            "slots_left": t.slots_left, "u": router.units(t.soc_pct), "sunlit": t.sunlit,
            "hk": t.hk_queued, "img": t.img_queued, "voice": t.voice_queued}


@app.get("/api/health")
def health():
    return {"status": "ok", "uptime_s": round(time.time() - STARTED, 1), "model_nodes": MODEL.meta.get("nodes"),
            "host": os.uname().nodename}


@app.get("/api/config")
def config():
    return {
        "ground": orbit.GROUND, "tle": orbit.load_tle(), "slot_s": orbit.SLOT_S,
        "actions": router.ACTIONS, "value": router.VALUE, "cost_pct": [c * router.UNIT_PCT for c in router.COST],
        "threshold_db": router.THRESH, "floor_pct": router.FLOOR_PCT, "reserve_pct": router.pct(router.RESERVE_UNITS),
        "model": MODEL.meta,
        "stand_in": "CroCube (NORAD 62394), a real 1U CubeSat. SomaiyaSat has not flown; its orbit and "
                    "telemetry here are borrowed from CroCube via the SatNOGS open network.",
    }


@lru_cache(maxsize=8)
def _passes(bucket: int, hours: int):
    start = orbit.now_utc().replace(microsecond=0)
    g = orbit.GROUND
    return {"computed_utc": start.isoformat().replace("+00:00", "Z"),
            "passes": orbit.find_passes(orbit.satrec(), g["lat"], g["lng"], start, hours, alt_km=g["alt_km"])}


@app.get("/api/passes")
def passes(hours: int = Query(default=36, ge=1, le=96)):
    return _passes(int(time.time() // 300), hours)       # recomputed at most every 5 minutes


@lru_cache(maxsize=1)
def _real():
    raw = json.loads((orbit.ROOT / "data" / "real" / "passes.json").read_text())
    stations, mix, beacons, messages = {}, {}, [], []
    for p in raw:
        s = stations.setdefault(p["station"], {"name": p["station"], "lat": p["lat"], "lng": p["lng"],
                                               "passes": 0, "frames": 0})
        s["passes"] += 1
        s["frames"] += p["frames_total"]
        for k, v in p["counts"].items():
            mix[k] = mix.get(k, 0) + v
        for b in p["beacons"]:
            f = b["fields"]
            if b["kind"] == "U":
                beacons.append({"t": b["t"], "station": p["station"], "trx_temp_c": f["uhf_trx_temp"] / 10,
                                "pa_temp_c": f["uhf_pa_temp"] / 10, "rssi_raw": f["uhf_act_rssi_raw"],
                                "uptime_s": f["uhf_uptime"], "resets": f["uhf_reset_cnt"],
                                "tx_count": f["uhf_tx_cnt"], "raw": f["uhf_beacon_raw"]})
            elif b["kind"] == "message":
                messages.append({"t": b["t"], "station": p["station"], "text": f["message"]})
    beacons.sort(key=lambda b: b["t"] or "")
    return {"source": "SatNOGS Network observations of CroCube (NORAD 62394), decoded with satnogs-decoders",
            "window": [raw[0]["start"], raw[-1]["end"]], "passes": len(raw),
            "stations": sorted(stations.values(), key=lambda s: -s["frames"]), "frame_mix": mix,
            "uhf_beacons": beacons, "messages": messages,
            "temp_scale_note": "temperatures assumed to be reported in tenths of a degree C"}


@app.get("/api/real")
def real():
    return _real()


@app.get("/api/metrics")
def metrics():
    return json.loads((orbit.ROOT / "model" / "metrics.json").read_text())


@app.post("/api/decide")
def decide(t: Telemetry):
    s = _state(t)
    a, source, reason = router.guarded_decision(MODEL, s, {"safe_mode": t.safe_mode})
    return {"action": router.ACTIONS[a], "source": source, "reason": reason,
            "baseline_action": router.ACTIONS[router.baseline_action(s)],
            "p_deliver": round(router.p_deliver(a, t.snr_db), 3)}


@app.post("/api/plan")
def plan(body: PlanIn):
    p = body.pass_.model_dump()
    n = len(p["elev"])
    ev = body.events.model_dump()
    for name, lst in ev.items():
        if lst is not None and len(lst) != n:
            raise HTTPException(422, f"events.{name} must have {n} entries (one per slot)")
    loss = ev["extra_loss_db"] or [0.0] * n
    if any(not 0 <= x <= 30 for x in loss):
        raise HTTPException(422, "extra_loss_db must be between 0 and 30 dB")
    if any(x is not None and not router.FLOOR_PCT <= x <= 100 for x in (ev["soc_set"] or [])):
        raise HTTPException(422, "soc_set must be between 20 and 100 percent")
    snr, dice = router.channel(n, body.seed, loss, p["range_km"])
    events = {k: dict(enumerate(v)) for k, v in ev.items() if v is not None and k != "extra_loss_db"}
    start = body.start.model_dump()
    ai, ai_tot = router.simulate(p, start, snr, dice, lambda s, f: router.guarded_decision(MODEL, s, f), events)
    shared = {k: events[k] for k in ("eclipse", "soc_set") if k in events}
    base, base_tot = router.simulate(p, start, snr, dice,
                                     lambda s, f: (router.baseline_action(s), "baseline", "fixed priority HK > IMG > VOICE"),
                                     shared)
    return {"slots": n, "ai": ai, "baseline": base, "totals": {"ai": ai_tot, "baseline": base_tot}}


@app.get("/")
def index():
    return FileResponse(orbit.ROOT / "web" / "index.html")


app.mount("/", StaticFiles(directory=orbit.ROOT / "web"), name="web")
