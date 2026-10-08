"""Fixed workload for the router service: N whole-pass plans at a set concurrency.

Every request replays one REAL pass (taken from /api/passes) with a seeded start
state, so the workload is identical from run to run. Standard library only.

Usage:  python tools/loadtest.py [url] [requests] [concurrency]
Prints one JSON line: throughput, latency percentiles and error count.
"""
import concurrent.futures as cf
import json
import random
import sys
import time
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 400
C = int(sys.argv[3]) if len(sys.argv) > 3 else 8


def call(path, body=None):
    req = urllib.request.Request(URL + path, json.dumps(body).encode() if body else None,
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    passes = call("/api/passes")["passes"]
    rng = random.Random(42)
    jobs = []
    for i in range(N):
        p = passes[i % len(passes)]
        jobs.append({"pass": {k: p[k] for k in ("elev", "range_km", "sunlit")}, "seed": i,
                     "start": {"soc": rng.randint(25, 95), "hk": rng.randint(2, 6),
                               "img": rng.randint(0, 16), "voice": rng.randint(0, 10)}})

    def one(body):
        t = time.perf_counter()
        try:
            call("/api/plan", body)
            return time.perf_counter() - t, True
        except Exception:
            return time.perf_counter() - t, False

    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(C) as pool:
        res = list(pool.map(one, jobs))
    wall = time.perf_counter() - t0
    lat = sorted(r[0] * 1000 for r in res)
    q = lambda f: round(lat[min(int(f * len(lat)), len(lat) - 1)], 1)
    print(json.dumps({"requests": N, "concurrency": C, "errors": sum(not r[1] for r in res),
                      "wall_s": round(wall, 2), "throughput_rps": round(N / wall, 1),
                      "latency_ms": {"mean": round(sum(lat) / len(lat), 1), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99)},
                      "slots_decided": sum(len(j["pass"]["elev"]) for j in jobs) * 2}))


if __name__ == "__main__":
    main()
