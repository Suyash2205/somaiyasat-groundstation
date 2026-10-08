# SomaiyaSat ground station — Cloud Computing AI use case (KJS-SRS-01)

A mission-control console for the SomaiyaSat PocketQube. During each ground pass an
onboard-sized AI router decides, every 15 seconds, whether to downlink housekeeping,
an image chunk, voice/data, or nothing, given the link strength, the battery and what
is queued. The console replays real passes on a 3D globe and runs the AI router side by
side with the traditional fixed-priority scheme.

## What is real and what is simulated

| Part | Source |
|---|---|
| Orbit, satellite position, pass times, sky track, eclipse | **Real.** SGP4 on the orbital elements of CroCube (NORAD 62394), a 1U CubeSat used as a stand-in because SomaiyaSat has not flown. Elements from the SatNOGS DB. |
| "Real telemetry" tab, ground-station dots on the globe | **Real.** 150 passes / 1,798 frames received by 59 volunteer stations on 2026-10-01, from the SatNOGS Network, decoded with `satnogs-decoders`. Cached in `data/real/`. |
| Link strength | **Modelled.** Free-space loss from the real slant range plus seeded random fading. |
| Battery, downlink queue, delivery outcome | **Simulated.** No battery beacon was downlinked in the captured window. |
| Training scenarios | **Synthetic** start states and fading on **real** pass geometry (`app/train.py`). Sample in `data/synthetic/training_sample.csv`. |

## Run it

```bash
# 1. With Docker (what the experiments use)
docker build -t suyash2215/somaiyasat-groundstation:1.0 .
docker run --rm -p 8088:8000 suyash2215/somaiyasat-groundstation:1.0
# open http://127.0.0.1:8088

# 2. Without Docker
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
# open http://127.0.0.1:8765
```

Click **Play next pass**, then use the Operator panel during the pass: change the link
interference, force an eclipse, drain the battery, break the model, corrupt a sensor
frame, or engage safe mode.

## Reproduce the model and the data

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tools/fetch_real_data.py 6 50     # refresh data/real from SatNOGS (no account needed)
.venv/bin/python -m app.train 900 300              # ~6 min; writes model/router_tree.json and model/metrics.json
.venv/bin/python tools/loadtest.py http://127.0.0.1:8088 400 8   # fixed workload used in experiments 7 and 8
```

## Layout

```
app/orbit.py     SGP4 pass prediction, look angles, eclipse
app/router.py    link model, fixed-priority baseline, fallback rule, tree runtime, watchdog, DP oracle, pass simulation
app/train.py     scenario generation, oracle labelling, training, evaluation
app/main.py      FastAPI service (see /docs for the API)
web/             dashboard (three.js globe, satellite.js); all libraries and fonts are vendored, works offline
model/           trained tree (JSON, 18 kB) and its evaluation
data/real/       cached SatNOGS orbital elements and decoded passes
tools/           data fetcher, load test, screenshot driver
```

## The AI component

- **Oracle:** a dynamic programme over (battery, queue) that knows the link for the whole
  pass. It labels the training data and gives an upper bound. It could not run on board.
- **Model:** a depth-10 decision tree (847 nodes) trained to imitate the oracle from ten
  causal inputs. Exported to flat arrays and evaluated with plain comparisons
  (about 2 µs per decision here), with no ML library at run time.
- **Watchdog:** rejects an out-of-range model output, a request to send from an empty
  queue, anything that would breach the 20 % battery floor, and housekeeping starvation
  at the end of a pass; it substitutes a conservative rule. Every decision is logged
  with its reason and the tree path.
- **Rule-based baseline:** fixed priority housekeeping > image > voice, blind to link and
  battery.

Results on 300 unseen passes are in `model/metrics.json` and in the **Router model** tab.

## Invalid input

`POST /api/decide` and `POST /api/plan` validate every field (ranges, per-slot list
lengths) and return HTTP 422 with the offending field. The **Corrupt sensor frame**
button sends a frame with `soc_pct = -40` and shows the rejection.

## Demo video

`demo/SomaiyaSat_demo.mp4` (2 min 47 s) walks through every control: globe drag and zoom,
the three tabs, two pass replays, pause and speeds, both sliders, and all five fault
buttons. It was recorded from the running app with `tools/record_demo.py`, which drives
the page with real mouse events. The pointer and the caption strip in the video are added
by that script for the recording; they are not part of the app.

## Live link

https://somaiyasat.happyflower-9030045b.centralindia.azurecontainerapps.io

The same Docker image, running on Azure Container Apps (0.25 vCPU, 0.5 GB, scales to zero).
The first visit after an idle period takes a few seconds while the container starts.
