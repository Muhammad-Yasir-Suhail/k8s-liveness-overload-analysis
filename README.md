# When Self-Healing Hurts

Reproducing signal-based health monitoring for Kubernetes and fixing probe-induced restart cascades

Cloud Computing, 100 Day Research Assignment. Group G18, Section A, FAST-NUCES Islamabad.
M. Yasir Suhail, M. Aashir Sheraz.

Status: Deliverable 02 (base-article implementation, research gap, proposed method, results).

| | |
|---|---|
| Base paper | Roberts, Archibald, Trinder. *Signalling Health for Improved Kubernetes Microservice Availability*, arXiv:2507.02158 (2025) |
| Report | `report/main.pdf` (LaTeX source in `report/`) |
| How to run | `docs/GUIDE.md` |
| Viva notes | `docs/VIVA_NOTES.md` |

## Summary

Kubernetes restarts a container when its liveness probe fails. If the health check waits in the same queue as real work, a busy container looks dead and gets killed. Its load moves to the other replicas, which then fail their probes too. The base paper studies one pod under light load, so it cannot show this.

What we did:

1. Re-implemented the paper's core method on stock Kubernetes (no modified kubelet) and reproduced its detection times and availability ranking.
2. Found a gap in the paper: no test with more than one replica, no distinction between "slow" and "dead", and no restart decision across replicas.
3. Proposed two components:
   - **A, progress-based liveness.** A container is alive if it has no pending work or has finished work in the last W seconds.
   - **B, cluster-aware restart governor.** It holds restarts back when many replicas fail together, because a restart cannot fix a shared cause.
4. Evaluated all four combinations (FP, FP+A, FP+B, FP+A+B) under sustained load, a load spike, a silent hang and a dependency stall.

## Results

All numbers are from `results/` and the generated tables in `report/generated/`. The simulator uses 10 to 30 seeded runs per cell. The testbed uses few runs (see the last column), so only large effects are reliable.

### Reproduction of the base paper (testbed, 3 runs per arm)

| Arm | Time to queue restart | Time to start restart | Paper (queue / start) |
|---|---|---|---|
| DP (default probes) | 7.9 s | 38.9 s | 8.7 s / 38.9 s |
| FP (fast probes) | 0.1 s | 2.5 s | 0.7 s / 2.9 s |
| SKI (signal-based) | 0.1 s | 2.1 s | 0.1 s / 2.2 s |

The ranking DP much worse than FP, and FP slightly worse than SKI, holds.

### Proposed method (testbed)

| Scenario | FP (baseline) | FP+A+B (proposed) | No probe | Runs per arm |
|---|---|---|---|---|
| Load spike to 150 %, success after the spike | 3.6 %, never recovers | 97.7 % | 95.5 % | 1 |
| 20 s dependency stall, overall success | 88.4 % | 92.1 % (0 restarts) | 92.2 % | 2 |
| Silent hang, time to detect | 0.9 s | 5.7 s | never | 2 |

The simulator shows the same ordering with larger gaps. For example, at 90 % load FP cuts success to 58.9 % while the no-probe control stays at 100 %.

### Findings that differ from the simulator or the paper

- The simulator predicts extra FP restarts caused by a race between container start-up and the first probe. The testbed did **not** reproduce them: FP restarted exactly once per run. We report this as an open hypothesis.
- On the testbed, effective capacity was below the calibrated value, so the nominal "90 % load" run is already slightly past saturation (no-probe success about 71 %). The ranking of the arms is the same as in the simulator.
- Component A alone does not help during a dependency stall. Component B is needed there.
- A detects a silent hang about W seconds later than the fast probe (5.7 s against 0.9 s on the testbed).

## Test environment

Laptop with an Intel Core i5-1135G7 (8 threads), about 9.6 GB of memory visible to WSL2, Docker 29.9.0, kind v0.23.0, Kubernetes 1.27.13, Python 3.10. Go is built inside Docker, so it is not needed on the host.

## Quick start

```bash
python3 sim/run_all.py                               # simulator, ~5 min
docker build -t g18/tools:latest src/
python3 experiments/local_check.py --docker          # real-process check, ~5 min
PROFILE=smoke bash experiments/run_all.sh            # Kubernetes testbed check, ~40 min
PROFILE=quick bash experiments/run_all.sh            # testbed results for the report, ~5 h
python3 analysis/analyze_testbed.py                  # fills the testbed tables
cd report && latexmk -pdf main.tex
```

Run the smoke profile first and read `results/testbed-smoke/`. A broken setup is much cheaper to find there than after five hours.

## Layout

```
src/          Go (stdlib only): cmd/sut, cmd/loadgen, cmd/ski, cmd/governor,
              internal/progress (Component A), internal/policy (Component B), internal/kube
manifests/    repro/ (SockShop catalogue: DP, FP, SKI arms), overload/ (SUT, governor), loadgen.yaml
cluster/      kind-single.yaml (default, K8s 1.27.13), kind-multi.yaml
experiments/  00_setup, 01_repro, 02_calibrate, 03_overload, 03b_spike, 04_hang, 05_stall,
              06_sweep (optional), run_all.sh, config.env, local_check.py
sim/          probesim.py (discrete-event simulator), run_all.py
analysis/     analyze_testbed.py
results/      sim/ and local/ (included), testbed/ (created by your run)
report/       main.tex, references.bib, figures/, generated/
docs/         GUIDE.md, VIVA_NOTES.md, cutoff1/ (proposal sources + original code)
```

## Known issues we found and fixed

- The catalogue manifests dropped all Linux capabilities, but the SockShop image gives `/app` a file capability, so the kernel refused to run it (`exec /app: operation not permitted`) and every reproduction run crash-looped. The manifests now keep `drop: ALL` and add `NET_BIND_SERVICE`.
- `analysis/analyze_testbed.py` stops on NUL bytes in `timeseries.csv`, which appear when a run is interrupted. The reader now strips them. Delete any run folder without `meta.json` and run it again.
- The Cutoff-1 code had three bugs (traffic sent to one pod through `port-forward`, a wall-clock CPU loop, and event attribution across runs). They are described in Section VII of the report.

## Limitations

- The paper's modified kubelet is not public, so SKI is emulated with a log sidecar, a shared PID namespace and a readiness gate.
- Testbed runs are few (1 to 3 per arm), so the simulator provides the statistics.
- Component A needs two hooks in the application and a free scheduler thread (`GOMAXPROCS` above the worker pool size).
- The governor can delay needed restarts by up to H seconds if many replicas hang at once from a bug in each pod.

## Tests

```bash
cd src && go test ./...      # Component A and B unit tests
```