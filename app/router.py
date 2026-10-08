"""SomaiyaSat downlink router: link model, policies, watchdog and pass simulation.

One pass is a sequence of 15 s slots. In each slot the satellite transmits one
unit of ONE traffic class (or stays idle):

  HK     housekeeping / TT&C frame   robust low-rate channel, cheap, most valuable
  IMG    SSTV-style image chunk      needs a strong link, expensive
  VOICE  M17 / Codec2 voice-data     in between
  IDLE   transmit nothing            saves battery

The downlink is blind (no acknowledgement), so a unit that is transmitted on a
weak link is simply lost. Battery is tracked in units of 0.25 % state of charge
above a hard 20 % floor.

Policies
  baseline_action   fixed priority HK > IMG > VOICE, ignores link and battery
                    (the "traditional" scheme the use case wants to improve on)
  fallback_action   conservative rule used by the watchdog when the model
                    cannot be trusted
  TreeModel         the trained decision tree, exported to plain arrays so it
                    runs without scikit-learn (and would run on a microcontroller)
  oracle_policy     dynamic programme with perfect knowledge of the link; used
                    only to label training data and as an upper bound
"""
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "model" / "router_tree.json"

HK, IMG, VOICE, IDLE = 0, 1, 2, 3
ACTIONS = ["HK", "IMG", "VOICE", "IDLE"]
VALUE = [10.0, 4.0, 2.0, 0.0]        # mission value of one delivered unit
COST = [1, 4, 2, 0]                  # battery units (0.25 % SoC) per slot
THRESH = [4.0, 13.0, 9.0]            # SNR (dB) for 50 % delivery, per class
SLOPE = 1.2                          # dB, steepness of the delivery curve
CHARGE = 1                           # battery units gained per sunlit slot
FLOOR_PCT = 20.0                     # hard battery floor
UNIT_PCT = 0.25
RESERVE_UNITS = 80                   # 40 % SoC: below this, energy is precious
W_BELOW, W_ABOVE = 1.0, 0.15         # end-of-pass value of one battery unit
HK_PERIOD = 4                        # a fresh housekeeping frame every 4 slots (60 s)
S_MAX, H_MAX, I_MAX, V_MAX = 200, 8, 16, 10
MAX_UNITS = int((100 - FLOOR_PCT) / UNIT_PCT)
SNR_ZENITH, REF_RANGE_KM = 20.0, 500.0
FEATURES = ["snr_db", "elev_deg", "rising", "max_elev_deg", "slots_left",
            "soc_pct", "sunlit", "hk_queued", "img_queued", "voice_queued"]


def pct(units):
    return FLOOR_PCT + units * UNIT_PCT


def units(soc_pct):
    return int(round((min(max(soc_pct, FLOOR_PCT), 100.0) - FLOOR_PCT) / UNIT_PCT))


def snr_from_range(range_km):
    """Free-space path loss relative to an overhead pass at 500 km."""
    return SNR_ZENITH - 20.0 * np.log10(np.asarray(range_km, dtype=float) / REF_RANGE_KM)


def p_deliver(action, snr_db):
    if action == IDLE:
        return 0.0
    return 1.0 / (1.0 + math.exp(-(snr_db - THRESH[action]) / SLOPE))


def battery_value(u):
    return W_BELOW * min(u, RESERVE_UNITS) + W_ABOVE * max(u - RESERVE_UNITS, 0)


def channel(n, seed, extra_loss_db=None, range_km=None):
    """Deterministic fade (AR(1), sigma 2 dB) and delivery dice for a pass."""
    rng = np.random.default_rng(seed)
    fade = np.zeros(n)
    x = rng.normal(0, 2.0)
    for t in range(n):
        x = 0.8 * x + rng.normal(0, 1.2)
        fade[t] = x
    dice = rng.random(n)
    snr = snr_from_range(range_km) + fade
    if extra_loss_db is not None:
        snr = snr - np.asarray(extra_loss_db, dtype=float)
    return snr, dice


# ---------------------------------------------------------------- policies

def baseline_action(s):
    if s["hk"] > 0:
        return HK
    if s["img"] > 0:
        return IMG
    if s["voice"] > 0:
        return VOICE
    return IDLE


def fallback_action(s):
    """Conservative rule: housekeeping always, payload only on a clearly good link with reserve."""
    if s["hk"] > 0 and s["u"] >= COST[HK]:
        return HK
    if s["u"] > RESERVE_UNITS:
        if s["img"] > 0 and s["snr"] >= THRESH[IMG] + 2:
            return IMG
        if s["voice"] > 0 and s["snr"] >= THRESH[VOICE] + 2:
            return VOICE
    return IDLE


def features(s):
    return [s["snr"], s["elev"], 1.0 if s["rising"] else 0.0, s["max_elev"], s["slots_left"],
            pct(s["u"]), 1.0 if s["sunlit"] else 0.0, s["hk"], s["img"], s["voice"]]


class TreeModel:
    """Decision tree stored as flat arrays; prediction is a handful of comparisons."""

    def __init__(self, path=MODEL_PATH):
        d = json.loads(Path(path).read_text())
        self.feature, self.threshold = d["feature"], d["threshold"]
        self.left, self.right, self.leaf = d["left"], d["right"], d["leaf"]
        self.meta = d.get("meta", {})

    def predict(self, x):
        node, path = 0, []
        while self.left[node] != -1:
            f, th = self.feature[node], self.threshold[node]
            go_left = x[f] <= th
            path.append((f, go_left, th))
            node = self.left[node] if go_left else self.right[node]
        return self.leaf[node], path


def explain(action, s, path=None):
    snr, soc = s["snr"], pct(s["u"])
    if action == HK:
        why = f"housekeeping goes first; link {snr:.1f} dB is enough for the robust channel"
    elif action == IMG:
        why = f"link {snr:.1f} dB carries image data and battery {soc:.0f}% is above reserve"
    elif action == VOICE:
        why = f"link {snr:.1f} dB is too weak for image, good enough for voice/data"
    elif s["hk"] + s["img"] + s["voice"] == 0:
        why = "queue empty"
    elif soc <= pct(RESERVE_UNITS) and s["hk"] == 0:
        why = f"battery {soc:.0f}% is at or below the 40% reserve; holding payload"
    else:
        why = f"link {snr:.1f} dB is too weak for what is queued; holding until it improves"
    if path:
        tail = ", ".join(f"{FEATURES[f]} {'<=' if le else '>'} {th:.1f}" for f, le, th in path[-3:])
        why += f"  [tree: {tail}]"
    return why


def guarded_decision(model, s, flags=None):
    """Model decision wrapped in the watchdog. Returns (action, source, reason).

    source is 'model', 'watchdog' or 'operator'. The watchdog never lets an
    anomalous output reach the radio: it substitutes the conservative rule.
    """
    flags = flags or {}
    if flags.get("safe_mode"):
        a = HK if s["hk"] > 0 and s["u"] >= COST[HK] else IDLE
        return a, "operator", "operator safe mode: housekeeping only"
    if flags.get("corrupt"):
        a = HK if s["hk"] > 0 else IDLE
        return a, "watchdog", "sensor frame failed validation; ignoring model, sending housekeeping only"
    raw, path = (97, None) if flags.get("model_fault") else model.predict(features(s))
    if raw not in (HK, IMG, VOICE, IDLE):
        a = fallback_action(s)
        return a, "watchdog", f"model output {raw} is not a valid action; fallback rule chose {ACTIONS[a]}"
    queued = [s["hk"], s["img"], s["voice"]]
    if raw != IDLE and queued[raw] == 0:
        a = fallback_action(s)
        return a, "watchdog", f"model asked for {ACTIONS[raw]} but that queue is empty; fallback chose {ACTIONS[a]}"
    if COST[raw] > s["u"]:
        a = fallback_action(s)
        return a, "watchdog", f"{ACTIONS[raw]} would take the battery below the 20% floor; fallback chose {ACTIONS[a]}"
    if s["hk"] > 0 and raw != HK and s["slots_left"] <= s["hk"] and s["u"] >= COST[HK]:
        return HK, "watchdog", "pass is ending with housekeeping still queued; forcing housekeeping"
    return raw, "model", explain(raw, s, path)


# ---------------------------------------------------------------- simulation

def simulate(pass_, start, snr, dice, decide, events=None):
    """Play one pass with a decision function. `events` holds optional per-slot overrides."""
    n = len(pass_["elev"])
    events = events or {}
    u = units(start["soc"])
    q = [min(int(start["hk"]), H_MAX), int(start["img"]), int(start["voice"])]
    max_elev = max(pass_["elev"])
    out, tot = [], {"value": 0.0, "sent": [0, 0, 0], "delivered": [0, 0, 0], "lost": [0, 0, 0],
                    "hk_generated": q[0], "idle": 0, "watchdog": 0}
    for t in range(n):
        if t > 0 and t % HK_PERIOD == 0 and q[0] < H_MAX:
            q[0] += 1
            tot["hk_generated"] += 1
        if events.get("soc_set", {}).get(t) is not None:
            u = units(events["soc_set"][t])
        sunlit = bool(pass_["sunlit"][t]) and not events.get("eclipse", {}).get(t, False)
        s = {"snr": float(snr[t]), "elev": pass_["elev"][t],
             "rising": t + 1 < n and pass_["elev"][t + 1] >= pass_["elev"][t],
             "max_elev": max_elev, "slots_left": n - t, "u": u, "sunlit": sunlit,
             "hk": q[0], "img": q[1], "voice": q[2]}
        flags = {k: events.get(k, {}).get(t, False) for k in ("safe_mode", "corrupt", "model_fault")}
        a, source, reason = decide(s, flags)
        if a != IDLE and (q[a] == 0 or COST[a] > u):      # radio brown-out guard
            a, reason = IDLE, "brown-out: battery at the 20% floor, transmitter disabled" if q[a] else "queue empty"
        ok = False
        if a == IDLE:
            tot["idle"] += 1
        else:
            q[a] -= 1
            u -= COST[a]
            ok = bool(dice[t] < p_deliver(a, s["snr"]))
            tot["sent"][a] += 1
            tot["delivered" if ok else "lost"][a] += 1
            if ok:
                tot["value"] += VALUE[a]
        if sunlit:
            u = min(u + CHARGE, MAX_UNITS)
        if source == "watchdog":
            tot["watchdog"] += 1
        out.append({"t": t, "action": ACTIONS[a], "ok": ok, "source": source, "reason": reason,
                    "soc": round(pct(u), 2), "q": list(q), "snr": round(s["snr"], 2), "sunlit": sunlit,
                    "p": round(p_deliver(a, s["snr"]), 3)})
    tot["soc_end"] = round(pct(u), 2)
    tot["battery_value"] = round(battery_value(u), 2)
    tot["score"] = round(tot["value"] + tot["battery_value"], 2)
    tot["left"] = list(q)
    return out, tot


# ---------------------------------------------------------------- oracle (training + upper bound)

def oracle_policy(n, snr, sunlit):
    """Backward dynamic programme over (battery, hk, img, voice) with the true link known.

    Rewards are EXPECTED delivered value, so the oracle knows the link but not the dice.
    Returns policy[t, s, h, i, v] -> action.
    """
    S, H, I, V = S_MAX + 1, H_MAX + 1, I_MAX + 1, V_MAX + 1
    s_idx = np.arange(S)
    vn = np.broadcast_to(np.array([battery_value(u) for u in s_idx])[:, None, None, None], (S, H, I, V)).copy()
    policy = np.zeros((n, S, H, I, V), dtype=np.int8)
    hmap = np.minimum(np.arange(H) + 1, H_MAX)
    for t in range(n - 1, -1, -1):
        ch = CHARGE if sunlit[t] else 0
        q = np.full((4, S, H, I, V), -np.inf)
        for a in (HK, IMG, VOICE, IDLE):
            ns = np.clip(s_idx - COST[a] + ch, 0, S_MAX)
            nxt = vn[ns]
            r = VALUE[a] * p_deliver(a, float(snr[t])) if a != IDLE else 0.0
            if a == HK:
                q[a][:, 1:] = r + nxt[:, :-1]
            elif a == IMG:
                q[a][:, :, 1:] = r + nxt[:, :, :-1]
            elif a == VOICE:
                q[a][:, :, :, 1:] = r + nxt[:, :, :, :-1]
            else:
                q[a] = nxt
            if COST[a]:
                q[a][:COST[a]] = -np.inf
        best = q.argmax(axis=0).astype(np.int8)
        w = q.max(axis=0)
        policy[t] = best                       # indexed by the queue AFTER any arrival
        if t > 0 and t % HK_PERIOD == 0:       # a fresh HK frame arrives before the decision
            w = w[:, hmap]
        vn = w
    return policy


def oracle_decider(policy):
    def decide(s, flags):
        t = policy.shape[0] - s["slots_left"]
        a = int(policy[t, min(s["u"], S_MAX), min(s["hk"], H_MAX), min(s["img"], I_MAX), min(s["voice"], V_MAX)])
        return a, "oracle", ""
    return decide
