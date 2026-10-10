# Project Handbook — Group G18

**When Self-Healing Hurts: An Empirical Study of Liveness-Probe-Induced Restart Cascades in Kubernetes Under Overload**

Cloud Computing — 100 Day Research Assignment
M. Yasir Suhail · M. Aashir Sheraz · Section A
Research Area: **Autonomous / Self-Adaptive Cloud Computing**

> This document is the complete record of the project: what it is, why it is defensible, how to execute it, and what to say when challenged. It is written to be self-contained — if you have this file and the repository, you have everything.

---

## Part 1 — The Idea

### 1.1 One-paragraph summary

Kubernetes restarts containers that fail their liveness probes, assuming a failed probe means a broken container. Under overload that assumption breaks: a saturated container is slow, not dead. If its health endpoint shares a thread pool or CPU quota with production traffic, queueing delay pushes probe response time past the timeout, and the kubelet kills a container that was still doing useful work. The dead replica's traffic redistributes onto the survivors, raising *their* health-endpoint latency — and the repair mechanism becomes the fault. This project measures where that boundary lies.

### 1.2 Primary research question

> Under sustained overload, at what combinations of Kubernetes liveness-probe parameters (`timeoutSeconds`, `periodSeconds`, `failureThreshold`) and container CPU limits does restart-based self-healing reduce service availability rather than restore it, and how large is the resulting degradation relative to a deployment configured with no liveness probe?

**SQ1** — How does offered load shift the instability boundary?
**SQ2** — Does cascade severity scale with replica count?

### 1.3 The four objectives

| | Objective | Delivered by |
|---|---|---|
| **O1** | Construct a reproducible testbed | Cutoff 2 |
| **O2** | Characterise the load–latency–probe coupling | Cutoff 2 (calibration) |
| **O3** | Locate the instability boundary | Cutoff 2–3 (sweep) |
| **O4** | Quantify the penalty and derive guidance | Cutoff 3 |

---

## Part 2 — Why This Is Defensible

**Read this section before any presentation or viva.** The single most likely challenge is "hasn't this been done?" — and the answer is specific, not vague.

### 2.1 The gap, precisely

| Prior work | What it does | Why the gap survives |
|---|---|---|
| **Roberts, Archibald & Trinder** — *Signalling Health for Improved Kubernetes Microservice Availability*, arXiv:2507.02158, 2025 | Models probe **detection speed**; proposes signal-based monitoring (SKI) as an alternative. Probes are the baseline, not the subject. | Evaluation uses **1 Pod per service on 1 node**. With one replica there is no load redistribution, so the feedback loop cannot form by construction. Failure is a single process kill at a fixed time. |
| **Flora, Gonçalves, Teixeira & Antunes** — IEEE Access vol. 10, 2022 | Finds probes **fail to detect** certain faults (business-logic errors, long response times) | Studies **false negatives**. This project studies **false positives**. Complementary, not overlapping. |
| **Zhou et al.** IEEE TSE 47(2), 2021 · **Silva et al.** SBES 2022 | Microservice fault taxonomies | Catalogue application- and infrastructure-level faults. Do **not** treat the orchestrator's health-check subsystem as a fault source. |
| **Heorhiadi et al.** (Gremlin) ICDCS 2016 | Systematic resilience testing | Injects faults into the application. Does not question the repair mechanism itself. |

### 2.2 The three facts that win the argument

These come from reading Roberts et al. in full. Memorise them.

1. **They hit the phenomenon and could not explain it.** Their aggressively tuned "fast probe" arm restarted the container ~3 times on average and lost ~4% availability, with kubelet logs attributing the extra restarts to failing liveness probes. The paper states it remains unclear why most of those restarts occur after the injected failure.

2. **Their setup makes cascades structurally impossible.** One Pod per service, one node, throughput capped. No redistribution path exists.

3. **Their future work is our research question.** They propose injecting network latency to emulate slow HTTP responses, and state they expect poll-based monitoring to erroneously detect high latency as container failure. **The prediction is on the published record. The experiment was never performed.**

### 2.3 The positioning sentence

Use this verbatim in the paper and in presentations:

> Roberts et al. observed erroneous probe-induced restarts as a side effect and identified latency-induced false detection as future work. We make it the object of study — under load, across the parameter space, with replica counts sufficient for cascade propagation.

### 2.4 Overlap with classmates

Checked against all 42 groups. Closest are:

- **G01** (HPA configurations under bursty load) — same *method shape*, entirely different subsystem. They tune the autoscaler; we tune the kubelet's health checker.
- **G22** (compound control-plane/data-plane faults) — both study self-healing breaking down, but they inject external faults with the control plane removed. Here the control plane works perfectly and the mechanism manufactures its own fault. Opposite direction.
- **G08** (single vs multi-agent recovery) — recovery *architecture* after a failure. We study failure *causation*.

**Action:** put one explicit sentence in Related Work distinguishing this from autoscaler control-loop tuning. That kills the ambiguity before a marker raises it.

---

## Part 3 — The Design, and Why Each Choice Was Made

Every decision below has a reason. If you change one, understand what it costs.

| Decision | Rationale | What breaks if changed |
|---|---|---|
| **`/healthz` shares a worker pool with `/work`** | This is the entire mechanism — how load becomes probe latency | Give `/healthz` its own goroutine and the phenomenon vanishes. **There is no project without this.** |
| **No faults injected** | The question is whether the orchestrator manufactures failures where none exist | Injecting faults makes this a replication of prior work |
| **CPU limit 200m** | The cgroup quota binds before the laptop does, so saturation is a property of the pod, not the machine | Without it you measure your hardware, and single-host testing becomes indefensible |
| **HPA disabled, replicas fixed at 6** | The probe loop must be the only adaptive mechanism running | With HPA on, you cannot attribute any effect to probes |
| **Readiness probe held constant** | Only liveness varies across arms | Varying readiness confounds the availability metric |
| **Control arm = no liveness probe at all** | The comparison that produces the headline result | Without it you can only rank configurations, not show harm |
| **Open-model load (constant arrival rate)** | Real clients keep sending when pods die | A closed model self-throttles and masks the cascade |
| **`periodSeconds` fixed at 5** | Keeps the factorial to 27 cells | Varying it adds a dimension without adding insight |
| **6 replicas, 3 worker nodes** | Enough for redistribution to matter | 1–2 replicas cannot cascade — this is exactly the flaw in the prior work |
| **5 repeats per cell** | Cascade onset is stochastic near the boundary | Fewer and you cannot separate signal from noise |

### The expected headline finding

A non-empty region of the parameter space where **no liveness probe is strictly better than a misconfigured one.** Counter-intuitive, quotable, and directly useful to practitioners.

---

## Part 4 — Execution Plan

### Cutoff 2 — Implementation and preliminary results

**Deliverables:** working testbed, calibration results, pilot sweep, at least one demonstrated cascade, draft methodology section.

| Week | Task | Done when |
|---|---|---|
| **1** | Cluster up, service built and deployed, observability running | `kubectl get pods` shows 6/6 ready, Prometheus scraping |
| **2** | **Induce a cascade reproducibly** ← the hard part | 3 consecutive runs at 150% load with `timeout=1s, threshold=1` all produce ≥3 probe-attributed restarts |
| **3** | Calibration + pilot sweep (`--pilot`) | `SATURATION_RATE` recorded; 30 pilot runs complete; heatmap renders |
| **4** | Fix whatever the pilot exposed; full sweep starts | Sweep running unattended overnight |

**Week 2 is the risk.** If cascades will not fire, in order: lower CPU limit to 100m → reduce `WORKERS` to 2 → raise load to 200% → drop `timeout` to 1s with `threshold` 1. One of these will work. The coupling is real; it is a matter of turning the gain up.

### Cutoff 3 — Full results and paper

| Week | Task |
|---|---|
| **5** | Full sweep completes (~26h machine time), re-run any failed cells |
| **6** | Analysis: heatmap, statistics, boundary identification |
| **7** | Write Results and Discussion |
| **8** | Threats to validity, Related Work, Conclusion, polish |

### Commands, in order

```bash
# --- setup ---
kind create cluster --config cluster/kind-config.yaml
cd service && docker build -t g18/sut:latest . && cd ..
kind load docker-image g18/sut:latest --name g18
kubectl apply -f manifests/service.yaml

# --- O2: calibration. DO THIS FIRST. ---
./experiments/calibrate.sh
# read the knee off the output, then:
#   edit experiments/config.env -> SATURATION_RATE=<measured value>

# --- pilot (smoke test, ~1.5h) ---
./experiments/run-sweep.sh --pilot
python3 analysis/analyze.py results/sweep-<stamp>

# --- full sweep (~26h, unattended) ---
./experiments/run-sweep.sh
python3 analysis/analyze.py results/sweep-<stamp>
```

---

## Part 5 — Repository Layout

```
.
├── service/            main.go, go.mod, Dockerfile   — the SUT
├── cluster/            kind-config.yaml
├── manifests/          deployment.template.yaml, service.yaml
├── load/               loadtest.js                   — k6, open model
├── experiments/        calibrate.sh, run-sweep.sh, config.env
├── analysis/           analyze.py                    — csv, heatmap, stats
├── results/            generated — commit these
├── paper/              main.tex, references.bib      — mirrors Overleaf
└── docs/               this handbook
```

**Commit `results/`.** Raw data in the public repo is a large part of the reproducibility claim.

---

## Part 6 — Metrics

| Metric | Source | Definition |
|---|---|---|
| Restart count | `timeseries.csv` | Container restarts attributable by kubelet event to liveness-probe failure |
| Availability | `timeseries.csv` | Time-weighted mean ready replicas ÷ 6 |
| Success rate | k6 | Requests completing without timeout or HTTP 5xx |
| P95 / P99 latency | k6 | Client-observed response time |
| Recovery time | `timeseries.csv` | First probe-induced restart → steady-state throughput restored |
| Cascade depth | `timeseries.csv` | Max replicas simultaneously unavailable |

The headline number is **Δ availability versus the matched no-probe control.** Negative means self-healing hurt.

---

## Part 7 — Threats to Validity

State these yourself in the paper. Volunteering limitations reads as rigour; being caught omitting them does not.

| Threat | Mitigation |
|---|---|
| Single-host cluster constrains scale | CPU limits bind before host capacity; saturation reported as **measured**, not assumed |
| Synthetic single service, not a microservice graph | Acknowledged. The mechanism (shared resource between health and work paths) is what generalises, not the application |
| `kind` is not production Kubernetes | Same kubelet, same probe implementation. The control loop under study is identical |
| Cascade onset is stochastic | 5 repeats per cell; non-parametric tests; report variance |
| Only one workload shape | Scoped explicitly in the title and RQ ("under overload") |

---

## Part 8 — Viva Preparation

Likely questions and the answers.

**"Isn't this just a misconfiguration problem?"**
Yes — and that is the point. Probe parameters are four integers copied between projects and rarely tuned. Everyone agrees they must be set "carefully." Nobody has published where careful ends. A known failure mode with no published boundary is exactly what empirical work is for.

**"Roberts et al. already looked at probes."**
They measured detection speed and proposed replacing probes. Their setup had one Pod per service on one node, so redistribution — the mechanism here — could not occur. They observed anomalous probe-induced restarts, said it was unclear why, and listed latency-induced false detection as future work. We are performing that experiment.

**"Why not use a real cloud cluster?"**
Cost and reproducibility. The CPU limit makes saturation a property of the pod, not the host, and every result is reproducible from a public repo on any laptop. A managed cluster would add noise we cannot control and prevent others from reproducing the work.

**"Isn't 6 replicas too few?"**
Six is enough for redistribution to shift measurable load onto survivors — which is the mechanism. The prior work used one. SQ2 addresses scaling explicitly.

**"What if you find nothing?"**
A bounded negative result is still a finding: it would place practical limits on a failure mode currently treated as a serious operational risk. Either outcome is publishable, which is why the design is low-risk.

**"How is this different from G01/G22?"**
G01 tunes the autoscaler control loop; we tune the health-check loop — different subsystem, different metrics. G22 injects external faults with the control plane degraded; here the control plane is healthy and the mechanism generates its own fault.

---

## Part 9 — References

1. J. Roberts, B. Archibald, P. Trinder, "Signalling Health for Improved Kubernetes Microservice Availability," arXiv:2507.02158, University of Glasgow, 2025.
2. J. Flora, P. Gonçalves, M. Teixeira, N. Antunes, "A Study on the Aging and Fault Tolerance of Microservices in Kubernetes," *IEEE Access*, vol. 10, pp. 132786–132799, 2022.
3. X. Zhou, X. Peng, T. Xie, J. Sun, C. Ji, W. Li, D. Ding, "Fault Analysis and Debugging of Microservice Systems: Industrial Survey, Benchmark System, and Empirical Study," *IEEE TSE*, vol. 47, no. 2, pp. 243–260, 2021.
4. F. Silva, V. Lelli, I. Santos, R. Andrade, "Towards a Fault Taxonomy for Microservices-Based Applications," SBES, 2022.
5. V. Heorhiadi, S. Rajagopalan, H. Jamjoom, M. K. Reiter, V. Sekar, "Gremlin: Systematic Resilience Testing of Microservices," ICDCS, Nara, Japan, 2016, pp. 57–66.
6. E. Casalicchio, "Container Orchestration: A Survey," in *Systems Modeling: Methodologies and Tools*, Springer, 2019, pp. 221–235.
7. M. Straesser, A. Bauer, R. Leppich, N. Herbst, K. Chard, I. Foster, S. Kounev, "An Empirical Study of Container Image Configurations and Their Impact on Start Times," CCGrid, Bangalore, 2023.
8. P. Garraghan, P. Townend, J. Xu, "An Empirical Failure-Analysis of a Large-Scale Cloud Computing Environment," HASE, Miami Beach, 2014, pp. 113–120.

**Add during Cutoff 2–3:** Kubernetes probe documentation (official), the DoorDash health-check incident report, and Colin Breck's probe analysis. Cite them as practitioner evidence that the phenomenon is real — which is exactly the framing you want, since their non-academic status is what establishes the gap.

---

## Part 10 — Outstanding Actions

- [ ] Instructor sign-off on area change (AI-Driven → Autonomous/Self-Adaptive)
- [ ] Create public GitHub repo, push this handbook and code
- [ ] Rename Overleaf project, upload `main.tex` + `references.bib`, compile
- [ ] Update Google Sheet row G18
- [ ] Submit Cutoff 1 PDF
- [ ] Verify institution name in `main.tex`
- [ ] Run Google Scholar / IEEE Xplore check on: `kubernetes liveness probe cascading failure`, `health check false positive container restart`, plus the "Cited by" list for arXiv:2507.02158
