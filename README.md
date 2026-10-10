# When Self-Healing Hurts

**Reproducing signal-based health monitoring for Kubernetes and fixing
probe-induced restart cascades**

Cloud Computing — 100 Day Research Assignment · Group **G18**, Section A ·
FAST-NUCES Islamabad · M. Yasir Suhail · M. Aashir Sheraz

**Status:** Deliverable 02 (base-article implementation + real gap +
proposed method).

| Base paper | Roberts, Archibald, Trinder, *Signalling Health for Improved Kubernetes Microservice Availability*, arXiv:2507.02158 (2025) |
|---|---|
| Report | [`report/main.pdf`](report/main.pdf) (LaTeX source in `report/`) |
| How to run | [`docs/GUIDE.md`](docs/GUIDE.md) |
| Viva notes | [`docs/VIVA_NOTES.md`](docs/VIVA_NOTES.md) |

## In one paragraph

Kubernetes restarts containers whose liveness probe fails. When the
health check waits in the same queue as real work, a busy container
looks dead and gets killed. Its load moves to the other replicas, which
then fail too. The base paper studies one pod under light load, so it
cannot see this. We reproduce the paper's results and explain its
unexplained extra restarts. We then propose two components:

- **A: progress-based liveness.** "Alive" means "finished some work in
  the last W seconds".
- **B: cluster-aware restart governor.** No mass restarts when many
  replicas fail together.

An ablation over overload, load spikes, silent hangs and dependency
stalls shows that A+B removes all false restarts and still catches real
hangs.

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

## Tests

```bash
cd src && go test ./...      # Component A and B unit tests
```
