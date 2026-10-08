"""Generate SYNTHETIC training scenarios, train the router tree, and evaluate it.

Pass geometry is real (SGP4 over random ground points with the cached TLE);
battery state, queue contents, fading and eclipse overrides are synthetic and
drawn from the ranges below. Labels come from the dynamic-programming oracle.

Usage:  python -m app.train [n_train_passes] [n_test_passes]
Writes: model/router_tree.json, model/metrics.json, data/synthetic/training_sample.csv
"""
import csv
import json
import sys
import time
from datetime import timedelta

import numpy as np
from sklearn.metrics import confusion_matrix
from sklearn.tree import DecisionTreeClassifier

from . import orbit
from .router import (ACTIONS, COST, FEATURES, HK, IDLE, MODEL_PATH, ROOT, TreeModel, baseline_action,
                     channel, fallback_action, features, guarded_decision, oracle_decider,
                     oracle_policy, simulate)


def random_passes(n, rng):
    sat = orbit.satrec()
    base = orbit.now_utc().replace(microsecond=0)
    out = []
    while len(out) < n:
        lat = float(np.degrees(np.arcsin(rng.uniform(-0.94, 0.94))))
        lng = float(rng.uniform(-180, 180))
        start = base + timedelta(hours=float(rng.uniform(0, 48)))
        for p in orbit.find_passes(sat, lat, lng, start, hours=24):
            if 8 <= len(p["elev"]) <= 56:
                out.append(p)
    rng.shuffle(out)
    return out[:n]


def random_scenario(p, rng):
    n = len(p["elev"])
    start = {"soc": float(rng.uniform(25, 95)), "hk": int(rng.integers(2, 7)),
             "img": int(rng.integers(0, 17)), "voice": int(rng.integers(0, 11))}
    sunlit = list(p["sunlit"]) if rng.random() > 0.3 else [False] * n
    loss = np.full(n, rng.uniform(0, 6) if rng.random() < 0.4 else 0.0)
    snr, dice = channel(n, int(rng.integers(1 << 31)), loss, p["range_km"])
    return dict(p, sunlit=sunlit), start, snr, dice


def collect(passes, rng, eps=0.15):
    X, y = [], []
    for p in passes:
        ps, start, snr, dice = random_scenario(p, rng)
        best = oracle_decider(oracle_policy(len(ps["elev"]), snr, ps["sunlit"]))

        def decide(s, flags):
            a = best(s, flags)[0]
            X.append(features(s))
            y.append(a)
            if rng.random() < eps:                      # wander off the optimal path
                opts = [k for k, qd in ((0, s["hk"]), (1, s["img"]), (2, s["voice"])) if qd and COST[k] <= s["u"]]
                a = int(rng.choice(opts + [IDLE]))
            return a, "explore", ""

        simulate(ps, start, snr, dice, decide)
    return np.array(X), np.array(y)


def export(clf, meta):
    t = clf.tree_
    leaf = [int(clf.classes_[v[0].argmax()]) for v in t.value]
    MODEL_PATH.parent.mkdir(exist_ok=True)
    MODEL_PATH.write_text(json.dumps({
        "feature": [int(f) for f in t.feature], "threshold": [round(float(x), 4) for x in t.threshold],
        "left": [int(x) for x in t.children_left], "right": [int(x) for x in t.children_right],
        "leaf": leaf, "features": FEATURES, "actions": ACTIONS, "meta": meta}))


def evaluate(passes, rng):
    model = TreeModel()
    deciders = {
        "fixed_priority_baseline": lambda s, f: (baseline_action(s), "baseline", ""),
        "rule_fallback_only": lambda s, f: (fallback_action(s), "rule", ""),
        "ai_tree_with_watchdog": lambda s, f: guarded_decision(model, s, f),
    }
    rows = {k: [] for k in list(deciders) + ["oracle_upper_bound"]}
    for p in passes:
        ps, start, snr, dice = random_scenario(p, rng)
        deciders["oracle_upper_bound"] = oracle_decider(oracle_policy(len(ps["elev"]), snr, ps["sunlit"]))
        for name, d in deciders.items():
            rows[name].append(simulate(ps, start, snr, dice, d)[1])
    summary = {}
    for name, r in rows.items():
        sent = np.array([x["sent"] for x in r]).sum(0)
        dl = np.array([x["delivered"] for x in r]).sum(0)
        summary[name] = {
            "mean_delivered_value": round(float(np.mean([x["value"] for x in r])), 2),
            "mean_score_incl_battery": round(float(np.mean([x["score"] for x in r])), 2),
            "hk_delivered_pct_of_generated": round(100 * dl[0] / sum(x["hk_generated"] for x in r), 1),
            "img_chunks_delivered_per_pass": round(dl[1] / len(r), 2),
            "voice_delivered_per_pass": round(dl[2] / len(r), 2),
            "transmissions_lost_pct": round(100 * (1 - dl.sum() / max(sent.sum(), 1)), 1),
            "mean_soc_end_pct": round(float(np.mean([x["soc_end"] for x in r])), 1),
            "passes_ending_below_25pct": int(sum(x["soc_end"] < 25 for x in r)),
            "watchdog_interventions_per_pass": round(float(np.mean([x["watchdog"] for x in r])), 2),
        }
    return summary


def main(n_train=900, n_test=300):
    rng = np.random.default_rng(7)
    t0 = time.time()
    passes = random_passes(n_train + 2 * n_test, rng)
    Xtr, ytr = collect(passes[:n_train], rng)
    Xte, yte = collect(passes[n_train:n_train + n_test], rng, eps=0.05)
    print(f"{len(Xtr)} training / {len(Xte)} test decisions in {time.time() - t0:.0f}s")

    clf = DecisionTreeClassifier(max_depth=10, min_samples_leaf=15, random_state=0).fit(Xtr, ytr)
    acc_tr, acc_te = clf.score(Xtr, ytr), clf.score(Xte, yte)
    meta = {"trained_utc": orbit.now_utc().isoformat(timespec="seconds"), "nodes": int(clf.tree_.node_count),
            "depth": int(clf.get_depth()), "train_decisions": len(Xtr), "train_passes": n_train}
    export(clf, meta)

    model = TreeModel()
    t1 = time.perf_counter()
    for x in Xte[:5000]:
        model.predict(list(x))
    us = (time.perf_counter() - t1) / min(len(Xte), 5000) * 1e6

    metrics = {
        "model": dict(meta, file_bytes=MODEL_PATH.stat().st_size, inference_microseconds=round(us, 2),
                      accuracy_vs_oracle_train=round(acc_tr, 4), accuracy_vs_oracle_test=round(acc_te, 4),
                      feature_importance=dict(zip(FEATURES, (round(float(v), 4) for v in clf.feature_importances_))),
                      confusion_matrix_rows_true_cols_pred={"labels": ACTIONS,
                                                            "matrix": confusion_matrix(yte, clf.predict(Xte), labels=[0, 1, 2, 3]).tolist()},
                      label_share_test=dict(zip(ACTIONS, (round(float((yte == k).mean()), 3) for k in range(4))))),
        "evaluation_passes": n_test,
        "policies": evaluate(passes[n_train + n_test:], rng),
        "note": "All figures are from SYNTHETIC scenarios on REAL pass geometry; none are measured on a satellite.",
    }
    (ROOT / "model" / "metrics.json").write_text(json.dumps(metrics, indent=1))
    out = ROOT / "data" / "synthetic"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "training_sample.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FEATURES + ["oracle_action"])
        for x, a in zip(Xtr[:3000], ytr[:3000]):
            w.writerow([round(float(v), 3) for v in x] + [ACTIONS[a]])
    print(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
