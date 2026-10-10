# Viva / mentor-meeting notes: Deliverable 02

## 1. Did we keep the base paper? Yes. Why?

**Paper:** Roberts, Archibald, Trinder, *Signalling Health for Improved
Kubernetes Microservice Availability*, arXiv:2507.02158, 2025.

What we checked before deciding:

| Check | Result |
|---|---|
| Official code / GitHub | **None.** SKI needs a *modified kubelet* that is not public. |
| Benchmark | SockShop catalogue + catalogue-db. Public images still on Docker Hub (amd64). |
| Configuration | Probe table given (Table II). `timeoutSeconds` and startup-probe values are missing. |
| Results to compare against | Tables III–VI: detection times, request counts, Ready-pod counts. |
| Hardware | 1 VM, 8 vCPU, 32 GB. Our single-pod setup fits an 8 GB laptop. |

Alternatives we looked at:
- **Flora et al. (IEEE Access 2022):** no code, and it studies false
  *negatives*.
- **Chen et al. (arXiv 2025):** no code, and it does not study probes.
- **Vayghan et al. (2019):** older, no artefact, no probes under load.

None of them is easier to reproduce, and none of them is about our
question.

**One-sentence answer:**
> We kept Roberts et al. because it is the closest work to our question.
> It saw our phenomenon (the extra fast-probe restarts) without
> explaining it, and it lists our experiment as future work. Its core
> method is small and fully specified. It has no code, so we
> re-implemented it on stock Kubernetes and reproduced its main tables.

## 2. What we reproduced (simulator now, testbed when run)

| | Paper | Our simulator |
|---|---|---|
| DP time to queue / start restart | 8.7 s / 38.9 s | 7.9 s / 38.1 s |
| FP time to queue / start restart | 0.7 s / 2.9 s | 0.8 s / 3.0 s |
| SKI time to queue / start restart | 0.1 s / 2.2 s | 0.1 s / 2.3 s |
| Successful requests DP / FP / SKI | 6890 / 26900 / 27989 | 6902 / 25961 / 28506 |
| Cumulative Ready pods DP / FP / SKI | 80 / 274 / 286 | 73 / 263 / 288 |

**Bonus: the paper's mystery.** The paper says "it remains unclear why
most restarts under FP occur after the failure". Here is why. FP probes
liveness 1 s after start with threshold 1, but the catalogue needs about
1.5 s to start listening. The first probe after a restart often fails,
and the new container is killed again. The kubelet's back-off makes the
first two such restarts free and adds 10 s, 20 s, ... from the third.
That also explains why the back-off looked "inconsistent" to the
authors.

## 3. The gap (say it in 30 seconds)

1. **G1, one pod hides the problem.** With one replica, load cannot move
   anywhere, so a probe restart cannot cascade. Our replica sweep shows
   FP is harmless at N=1 (93.5 % success) and harmful at N≥2 (down to
   about 20 % from N=4).
2. **G2, slow is treated as dead.** Poll-based probes use response time.
   The paper *predicts* false detection under latency but never tests
   it. SKI needs the app to log its own failure, so it misses silent
   hangs.
3. **G3, no aggregation.** Both the kubelet and SKI decide per
   container. The paper names this as a limitation itself.

## 4. Our method

- **A, progress-based liveness:**
  `alive = (nothing pending) OR (something finished in the last W s)`.
  It is served outside the worker pool, so it is never queued. An
  overloaded replica keeps finishing work, so it is alive. A hung replica
  finishes nothing, so it is dead after W.
- **B, cluster-aware restart governor:** it looks at all replicas.
  - If ≥ 50 % fail together, the cause is shared, so restarts are held
    for 60 s.
  - At most 25 % of replicas may be restarting at once.
  - If a restart does not help, the hold time is doubled.
  - A single hung pod is restarted immediately.

**Why both?**

| Scenario | A alone | B alone | A+B |
|---|---|---|---|
| Sustained 90 % load (no fault) | fixed | still restarts (11/run) | fixed |
| 60 s spike to 150 % | fixed | mostly fixed | fixed |
| Silent hang of one pod | detected in 5.4 s | detected in 1.5 s | detected in 5.4 s |
| 20 s dependency stall | 14 false restarts | 0.8 | 0 |

**Headline numbers (simulator):**
- 90 % load, nothing broken: success is 58.9 % with FP, 100 % with A+B,
  and 100 % with no probe at all.
- After a 60 s spike: FP stays at 4.6 % success (metastable failure),
  while A+B is back to 99.3 %.
- 20 s dependency stall: success is 66.0 % with FP and 92.2 % with A+B,
  which equals the best possible result.

## 5. Questions to expect

**"Why not just isolate the health endpoint?"**
That is our ISO arm. It also avoids false positives, but it **never**
detects a silent hang (0/20 in H). A keeps hang detection.

**"A is just a longer timeout."**
No. A longer timeout still measures latency, and under sustained
overload the queue wait has no upper bound. A measures *progress*.
Overload never trips A, at any W (Table XI, last column).

**"Isn't B dangerous if all pods really hang?"**
It delays restarts by at most H (60 s, doubled when restarts are futile,
capped at 300 s). This is the stated remaining limitation.

**"Why simulate?"**
Three reasons:
1. 30 seeded runs per arm, and a 27-cell sweep, would take days on a
   laptop.
2. The simulator reproduces the paper first, so it is calibrated.
3. Every scenario is also scripted for the real testbed.

We also measured the core mechanism on real processes
(`experiments/local_check.py`).

**"Your loads changed from Cutoff 1 (50/100/150 %)."**
Above about 110 %, every arm collapses, including no-probe, because
requests time out in the queue. The informative region is just below
saturation and during short spikes. The full 50–150 % grid is still in
the simulator (Fig. 8).

**"Real-world caveat you found?"**
If Go runs with GOMAXPROCS=1, a saturated process delays even `/livez`.
In pods Go uses the host CPU count, so this is fine, but it is a
deployment requirement for A.

## 6. Bugs we found in our own Cutoff-1 code (shows rigour)

1. `kubectl port-forward svc/...` sends ALL traffic to ONE pod, so there
   was no load redistribution at all.
2. `burn()` used a wall-clock deadline, so the CPU limit never limited
   capacity.
3. Liveness and readiness events were mixed, and events leaked between
   runs.
