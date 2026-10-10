#!/usr/bin/env python3
"""
probesim.py -- discrete-event simulator of Kubernetes health checking
Group G18, Deliverable 02

What it models (one object per real mechanism):

  * Replica       a container with a bounded worker pool (FIFO queue, like a
                  Go channel) and a CPU limit. CPU is shared equally between
                  the requests that are computing (processor sharing).
  * Kubelet       liveness / readiness probe workers (period, timeout,
                  threshold, initial delay), kill -> grace -> restart with
                  the documented CrashLoopBackOff (0, 10, 20, 40 ... 300 s).
  * kube-proxy    random choice among READY endpoints; endpoint changes
                  reach the proxy after a short propagation delay.
  * Clients       open model (Poisson arrivals, keep coming when pods die)
                  or closed model (users with a fixed pace, like Locust).
  * Faults        R: the app process dies but its wrapper shell stays
                     (Roberts et al.); H: one replica hangs silently;
                  D: the shared dependency stalls (every replica blocks).
  * Component A   progress-based liveness  (alive <=> nothing pending OR
                  something finished within W seconds)
  * Component B   cluster-aware restart governor (same rules as the Go
                  code in src/internal/policy -- see Governor below)

It is deliberately simple and fully seeded, so every number in the report
can be regenerated with `python3 sim/run_all.py`.
"""
from __future__ import annotations

import heapq
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    path: str            # 'healthz' (shared pool) | 'livez' (A) | 'naive' | 'app'
    period: float
    timeout: float
    threshold: int
    initial_delay: float
    result_delay: float = 0.0   # kubelet processing / reporting latency


@dataclass
class GovCfg:
    path: str = "livez"
    period: float = 1.0
    timeout: float = 1.0
    threshold: int = 1
    budget_frac: float = 0.25
    corr_frac: float = 0.5
    hold: float = 60.0
    hold_max: float = 300.0
    futile: float = 30.0
    grace: float = 10.0
    inflight_max: float = 120.0


@dataclass
class Cfg:
    scenario: str = "O"          # R | O | H | D
    arm: str = "FP"
    seed: int = 1
    n: int = 6
    workers: int = 4
    cpu: float = 0.2             # cores
    work_cpu: float = 0.010      # core-seconds per /work request
    health_cpu: float = 0.00005
    dep_latency: float = 0.001
    window: float = 5.0          # Component A stall window W
    # load
    mode: str = "open"           # open | closed
    rate: float = 120.0
    burst: Optional[tuple] = None   # (start_s, length_s, rate) relative to load start
    users: int = 100
    pace: float = 1.0
    client_timeout: float = 2.0
    warmup: float = 30.0
    duration: float = 240.0
    # probes
    liveness: Optional[Probe] = None
    readiness: Optional[Probe] = None
    governor: Optional[GovCfg] = None
    ski: bool = False
    ski_latency: float = 0.1
    ski_kill_delay: float = 2.0
    # lifecycle
    grace: float = 0.05          # SIGTERM -> exit (Go app exits at once)
    kill_grace: float = 2.0      # R only: wrapper shell ignores SIGTERM -> full grace
    start_min: float = 0.2       # process start -> listening (uniform)
    start_max: float = 0.4
    restart_overhead: float = 0.2
    pleg_max: float = 1.0        # self-exit detection (PLEG relist 1 s)
    ep_min: float = 0.5          # endpoint propagation
    ep_max: float = 1.5
    # faults
    inject_at: float = 60.0
    stall: float = 20.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class Item:
    """A request (work) or a probe waiting for / holding a pool slot."""
    __slots__ = ("kind", "t0", "deadline", "state", "rem", "gen", "user", "done", "cb")

    def __init__(self, kind, t0, deadline, gen, user=-1, cb=None):
        self.kind = kind          # 'work' | 'probe'
        self.t0 = t0
        self.deadline = deadline
        self.state = "queued"     # queued | dep | cpu | finished | dead
        self.rem = 0.0
        self.gen = gen
        self.user = user
        self.done = False         # client already has an outcome
        self.cb = cb              # probe callback(ok: bool)


class Backoff:
    """Kubelet restart back-off, modelled on the Kubernetes v1.27 source
    (pkg/kubelet/kuberuntime/kuberuntime_manager.go: doBackOff, and
    client-go flowcontrol.Backoff): initial 10 s, doubling, max 300 s,
    an entry expires when two exits are more than 600 s apart.

    The important detail: for a LIVENESS kill the kubelet kills and restarts
    the container in the SAME sync, using the pod status read BEFORE the
    kill. The back-off check therefore looks at the PREVIOUS exited
    instance, so the first two liveness restarts are free and back-off only
    bites from the third one. For a container that exits by itself the
    check uses the instance that just exited. While in back-off the pod
    worker retries every 10-15 s (backOffPeriod with 0.5 jitter)."""

    def __init__(self):
        self.backoff = None
        self.last = None

    def _expired(self, ts):
        return self.last is not None and ts - self.last > 600

    def in_backoff(self, ts, now):
        if self.backoff is None or self._expired(ts):
            return False
        return now - ts < self.backoff

    def next(self, ts):
        if self.backoff is None or self._expired(ts):
            self.backoff = 10.0
        else:
            self.backoff = min(300.0, 2 * self.backoff)
        self.last = ts


class Replica:
    def __init__(self, sim, idx):
        self.sim, self.idx = sim, idx
        self.gen = 0
        self.container = "running"   # running | terminating | exited
        self.app = False             # app process listening
        self.hung = False
        self.started = 0.0
        self.queue = deque()
        self.busy = 0
        self.cpu_jobs = []
        self.last = 0.0
        self.ver = 0
        self.pending = 0             # Component A tracker
        self.last_progress = 0.0
        self.ready = False
        self.in_ep = False
        self.ep_token = 0
        self.backoff = Backoff()
        self.restarts = 0
        self.live_fail = 0
        self.live_hold = False
        self.live_busy = False
        self.ready_fail = 0
        self.ready_busy = False
        self.kill_times = []
        self.start_times = []
        self.exits = []

    # ---- CPU (processor sharing) --------------------------------------
    def advance(self, now):
        if self.cpu_jobs and not self.hung:
            share = self.sim.cfg.cpu / len(self.cpu_jobs) * (now - self.last)
            for j in self.cpu_jobs:
                j.rem -= share
        self.last = now

    def reschedule(self, now):
        self.ver += 1
        if self.hung or not self.cpu_jobs:
            return
        m = min(j.rem for j in self.cpu_jobs)
        dt = max(0.0, m) * len(self.cpu_jobs) / self.sim.cfg.cpu
        self.sim.at(now + dt, self.on_cpu_done, self.ver)

    def on_cpu_done(self, now, ver):
        if ver != self.ver:
            return
        self.advance(now)
        fin = [j for j in self.cpu_jobs if j.rem <= 1e-12]
        self.cpu_jobs = [j for j in self.cpu_jobs if j.rem > 1e-12]
        for j in fin:
            self.finish(now, j)
        self.admit(now)
        self.reschedule(now)

    # ---- tracker (Component A) ----------------------------------------
    def begin(self, now):
        self.pending += 1
        if self.pending == 1:
            self.last_progress = now

    def end(self, now, completed):
        if completed:
            self.last_progress = now
        self.pending -= 1

    def livez(self, now):
        return self.pending <= 0 or (now - self.last_progress) < self.sim.cfg.window

    # ---- pool -----------------------------------------------------------
    def enqueue(self, now, item):
        if item.kind == "work":
            self.begin(now)
        self.queue.append(item)
        self.admit(now)

    def admit(self, now):
        cfg = self.sim.cfg
        changed = False
        while self.busy < cfg.workers and self.queue:
            it = self.queue.popleft()
            if it.state == "dead":
                continue
            self.busy += 1
            if it.kind == "probe":
                self.advance(now)
                it.state, it.rem = "cpu", cfg.health_cpu
                self.cpu_jobs.append(it)
                changed = True
            else:
                it.state = "dep"
                t = now + cfg.dep_latency
                s0, s1 = self.sim.stall_window
                if s0 <= now < s1:
                    t = s1 + cfg.dep_latency
                self.sim.at(t, self.on_dep_done, it)
        if changed:
            self.reschedule(now)

    def on_dep_done(self, now, it):
        if it.gen != self.gen or it.state != "dep":
            return
        self.advance(now)
        it.state, it.rem = "cpu", self.sim.cfg.work_cpu
        self.cpu_jobs.append(it)
        self.reschedule(now)

    def finish(self, now, it):
        it.state = "finished"
        self.busy -= 1
        if it.kind == "work":
            self.end(now, True)
            if not it.done:
                it.done = True
                self.sim.record(it, now, "o")
        else:
            if not it.done:
                it.done = True
                it.cb(now, True)

    def abandon(self, now, it):
        """client / prober gave up while the item waited in the queue"""
        if it.gen != self.gen:
            return
        if it.state == "queued":
            it.state = "dead"
            if it.kind == "work":
                self.end(now, False)

    # ---- lifecycle ------------------------------------------------------
    def fail_all(self, now):
        for it in list(self.queue) + self.cpu_jobs:
            if not it.done and it.state != "dead":
                it.done = True
                if it.kind == "work":
                    self.sim.record(it, now, "e")
                else:
                    it.cb(now, False)
        # dep-phase items are lost too (connection reset)
        self.queue.clear()
        self.cpu_jobs = []
        self.busy = 0
        self.ver += 1

    def kill(self, now, reason):
        """kubelet decided to restart (liveness / SKI)."""
        if self.container != "running":
            return
        cfg = self.sim.cfg
        self.container = "terminating"
        self.live_hold = True
        self.kill_times.append((now, reason))
        g = cfg.grace if self.sim.app_handles_sigterm else self.sim.kill_grace
        prev = self.exits[-1] if self.exits else None   # stale pod status
        self.sim.at(now + g, self.on_exit, self.gen, "kill", prev)

    def self_exit(self, now):
        """process exits by itself (governor /admin/exit)."""
        if self.container != "running":
            return
        self.kill_times.append((now, "governor"))
        self.container = "terminating"
        self.live_hold = True
        self.sim.at(now + 0.05, self.on_exit, self.gen, "self", None)

    def on_exit(self, now, gen, how, prev):
        if gen != self.gen:
            return
        cfg = self.sim.cfg
        self.app = False
        self.fail_all(now)
        self.container = "exited"
        self.set_ready(now, False)
        self.exits.append(now)
        if how == "kill":
            # same sync as the kill: back-off judged on the previous instance
            self.sim.at(now + cfg.restart_overhead, self.try_start, prev, self.gen)
        else:
            # PLEG notices the exit within ~1 s, then a sync runs
            self.sim.at(now + self.sim.rng.uniform(0, cfg.pleg_max), self.try_start, now, self.gen)

    def try_start(self, now, ts, gen):
        if gen != self.gen or self.container != "exited":
            return
        if ts is not None:
            if self.backoff.in_backoff(ts, now):
                # CrashLoopBackOff: pod worker retries after 10-15 s, and the
                # retry sees the latest exited instance
                self.sim.at(now + self.sim.rng.uniform(10, 15), self.try_start, self.exits[-1], gen)
                return
            self.backoff.next(ts)
        self.on_restart(now)

    def on_restart(self, now):
        cfg = self.sim.cfg
        self.gen += 1
        self.container = "running"
        self.hung = False
        self.started = now
        self.restarts += 1
        self.start_times.append(now)
        self.pending = 0
        self.last_progress = now
        self.live_fail = 0
        self.live_hold = False
        self.ready_fail = 0
        self.sim.at(now + self.sim.rng.uniform(cfg.start_min, cfg.start_max), self.on_app_up, self.gen)
        if cfg.ski:
            self.sim.ski_watch(self, now)

    def on_app_up(self, now, gen):
        if gen != self.gen or self.container != "running":
            return
        self.app = True
        self.last_progress = now
        if self.sim.cfg.readiness is None and not self.sim.cfg.ski:
            self.set_ready(now, True)

    def set_ready(self, now, val):
        if self.ready == val:
            return
        self.ready = val
        self.ep_token += 1
        cfg = self.sim.cfg
        self.sim.at(now + self.sim.rng.uniform(cfg.ep_min, cfg.ep_max), self.on_ep, val, self.ep_token)

    def on_ep(self, now, val, tok):
        if tok == self.ep_token:
            self.in_ep = val
            self.sim.ep_dirty = True


# ---------------------------------------------------------------------------
# Component B: governor (mirror of src/internal/policy/policy.go)
# ---------------------------------------------------------------------------

class Governor:
    def __init__(self, sim, g: GovCfg):
        self.sim, self.g = sim, g
        self.hold_start = None
        self.hold_len = g.hold
        self.inflight = {}
        self.recovered = {}
        self.fails = {}
        self.healthy = {}
        self.gens = {}
        self.held_ticks = 0

    def decide(self, now, pods):
        g = self.g
        n = len(pods)
        for (name, running, age, fails, healthy) in pods:
            if name in self.inflight:
                if running and healthy and age >= 1.0:
                    del self.inflight[name]
                    self.recovered[name] = now
                elif now - self.inflight[name] > g.inflight_max:
                    del self.inflight[name]
        suspects = []
        for (name, running, age, fails, healthy) in pods:
            if not running or age < g.grace:
                continue
            if fails >= g.threshold:
                suspects.append((name, fails))
                if name in self.recovered:
                    if now - self.recovered[name] <= g.futile:
                        self.hold_len = min(2 * self.hold_len, g.hold_max)
                    del self.recovered[name]
        if not suspects:
            self.hold_start = None
            return [], False
        frac = len(suspects) / n
        expired = False
        if frac >= g.corr_frac and len(suspects) >= 2:
            if self.hold_start is None:
                self.hold_start = now
            if now - self.hold_start < self.hold_len:
                return [], True
            expired = True
        else:
            self.hold_start = None
        budget = max(1, int(math.floor(g.budget_frac * n))) - len(self.inflight)
        if budget <= 0:
            return [], False
        out = []
        for name, f in sorted(suspects, key=lambda x: -x[1]):
            if budget == 0:
                break
            if name in self.inflight:
                continue
            out.append(name)
            self.inflight[name] = now
            budget -= 1
        if out and expired:
            self.hold_start = now
        return out, False

    def tick(self, now):
        sim, g = self.sim, self.g
        self.results = {}
        outstanding = 0
        for r in sim.reps:
            if r.container != "running":
                continue
            if g.path == "livez":
                self.results[r.idx] = r.app and r.livez(now)
            elif g.path == "healthz":
                if not r.app:
                    self.results[r.idx] = False
                    continue
                outstanding += 1

                def cb(t, ok, idx=r.idx):
                    self.results[idx] = ok
                it = Item("probe", now, now + g.timeout, r.gen, cb=cb)
                r.enqueue(now, it)
                sim.at(now + g.timeout, sim.on_probe_timeout, r, it)
        delay = g.timeout if outstanding else 0.002
        sim.at(now + delay, self.evaluate)
        sim.at(now + g.period, self.tick)

    def evaluate(self, now):
        sim = self.sim
        pods = []
        for r in sim.reps:
            running = r.container == "running"
            key = r.idx
            if self.gens.get(key) != r.gen:
                self.gens[key] = r.gen
                self.fails[key] = 0
                self.healthy[key] = False
            if running:
                ok = self.results.get(key, False)
                if ok:
                    self.fails[key], self.healthy[key] = 0, True
                else:
                    self.fails[key] += 1
                    self.healthy[key] = False
            pods.append((key, running, now - r.started, self.fails[key], self.healthy[key]))
        restart, held = self.decide(now, pods)
        if held:
            self.held_ticks += 1
        for idx in restart:
            r = sim.reps[idx]
            if r.container == "running":
                r.self_exit(now)


# ---------------------------------------------------------------------------
# the simulator
# ---------------------------------------------------------------------------

class Sim:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.q = []
        self.seq = 0
        self.now = 0.0
        self.reps = [Replica(self, i) for i in range(cfg.n)]
        self.ep_dirty = True
        self._ep = []
        self.records = []        # (t_send, latency, outcome)
        self.samples = []        # (t, ready, running_apps)
        self.stall_window = (1e18, 1e18)
        self.t_load = cfg.warmup
        self.t_end = cfg.warmup + cfg.duration
        self.t_inject = cfg.warmup + cfg.inject_at
        # catalogue (R) runs under a shell that ignores SIGTERM -> full grace
        self.app_handles_sigterm = cfg.scenario != "R"
        self.kill_grace = cfg.kill_grace
        self.gov = Governor(self, cfg.governor) if cfg.governor else None
        self.ski_events = []

    # ---- event queue ------------------------------------------------------
    def at(self, t, fn, *args):
        self.seq += 1
        heapq.heappush(self.q, (t, self.seq, fn, args))

    def run(self):
        cfg = self.cfg
        # all pods are created at t=0
        for r in self.reps:
            r.restarts = -1           # the first start is not a restart
            r.on_restart(0.0)
        for r in self.reps:
            if cfg.liveness:
                self.at(self.rng.uniform(0, cfg.liveness.period), self.live_tick, r)
            if cfg.readiness:
                self.at(self.rng.uniform(0, cfg.readiness.period), self.ready_tick, r)
        if self.gov:
            self.at(self.rng.uniform(0, cfg.governor.period), self.gov.tick)
        # load
        if cfg.mode == "open":
            self.at(self.t_load + self.rng.expovariate(cfg.rate), self.arrive)
        else:
            for u in range(cfg.users):
                self.at(self.t_load + self.rng.uniform(0, cfg.pace), self.user_send, u)
        # faults
        if cfg.scenario == "R":
            self.at(self.t_inject, self.inject_app_death)
        elif cfg.scenario == "H":
            self.at(self.t_inject, self.inject_hang)
        elif cfg.scenario == "D":
            self.stall_window = (self.t_inject, self.t_inject + cfg.stall)
        # sampling once per second during the load window (inclusive)
        for s in range(int(cfg.duration) + 1):
            self.at(self.t_load + s, self.sample)
        end = self.t_end + 5
        while self.q:
            t, _, fn, args = heapq.heappop(self.q)
            if t > end:
                break
            self.now = t
            fn(t, *args)
        return self

    # ---- endpoints / routing -------------------------------------------
    def endpoints(self):
        if self.ep_dirty:
            self._ep = [r for r in self.reps if r.in_ep]
            self.ep_dirty = False
        return self._ep

    def route(self, now, item):
        ep = self.endpoints()
        if not ep:
            return False
        r = ep[self.rng.randrange(len(ep))]
        if r.container != "running" or not r.app:
            return False          # connection refused / reset
        item.gen = r.gen
        r.enqueue(now, item)
        self.at(item.deadline, self.on_client_timeout, r, item)
        return True

    # ---- clients ------------------------------------------------------------
    def rate_at(self, now):
        b = self.cfg.burst
        if b:
            rel = now - self.t_load
            if b[0] <= rel < b[0] + b[1]:
                return b[2]
        return self.cfg.rate

    def arrive(self, now):
        if now >= self.t_end:
            return
        cfg = self.cfg
        it = Item("work", now, now + cfg.client_timeout, 0)
        if not self.route(now, it):
            it.done = True
            self.record(it, now, "e")
        self.at(now + self.rng.expovariate(self.rate_at(now)), self.arrive)

    def user_send(self, now, u):
        if now >= self.t_end:
            return
        cfg = self.cfg
        it = Item("work", now, now + cfg.client_timeout, 0, user=u)
        if not self.route(now, it):
            it.done = True
            self.record(it, now, "e")

    def record(self, it, now, outcome):
        if it.t0 >= self.t_load:
            self.records.append((it.t0 - self.t_load, now - it.t0, outcome))
        if it.user >= 0:
            nxt = max(now, it.t0 + self.cfg.pace)
            self.at(nxt, self.user_send, it.user)

    def on_client_timeout(self, now, r, it):
        if it.done:
            return
        it.done = True
        self.record(it, now, "t")
        r.abandon(now, it)

    def on_probe_timeout(self, now, r, it):
        if it.done:
            return
        it.done = True
        r.abandon(now, it)
        it.cb(now, False)

    # ---- kubelet probes ---------------------------------------------------
    def probe(self, now, r, spec, cb):
        """Run one probe against replica r; cb(t, ok) is called with the result."""
        if spec.path == "app":
            ok = r.app and not r.hung
            self.at(now + spec.result_delay, cb, ok)
            return
        if not r.app:
            self.at(now + 0.001 + spec.result_delay, cb, False)
            return
        if spec.path == "livez":
            self.at(now + 0.001 + spec.result_delay, cb, r.livez(now))
        elif spec.path == "naive":
            self.at(now + 0.001 + spec.result_delay, cb, True)
        else:  # healthz: shared FIFO pool
            it = Item("probe", now, now + spec.timeout, r.gen,
                      cb=lambda t, ok: self.at(t + spec.result_delay, cb, ok))
            r.enqueue(now, it)
            self.at(now + spec.timeout, self.on_probe_timeout, r, it)

    def live_tick(self, now, r):
        spec = self.cfg.liveness
        self.at(now + spec.period, self.live_tick, r)
        if (r.container != "running" or r.live_hold or r.live_busy
                or now - r.started < spec.initial_delay):
            return
        r.live_busy = True
        gen = r.gen

        def cb(t, ok):
            r.live_busy = False
            if gen != r.gen or r.live_hold:
                return
            if ok:
                r.live_fail = 0
            else:
                r.live_fail += 1
                if r.live_fail >= spec.threshold:
                    r.kill(t, "liveness")
        self.probe(now, r, spec, cb)

    def ready_tick(self, now, r):
        spec = self.cfg.readiness
        self.at(now + spec.period, self.ready_tick, r)
        if r.container != "running":
            return
        if r.ready_busy:
            return
        if now - r.started < spec.initial_delay:
            return
        r.ready_busy = True
        gen = r.gen

        def cb(t, ok):
            r.ready_busy = False
            if gen != r.gen:
                return
            if ok:
                r.ready_fail = 0
                r.set_ready(t, True)
            else:
                r.ready_fail += 1
                if r.ready_fail >= spec.threshold:
                    r.set_ready(t, False)
        self.probe(now, r, spec, cb)

    # ---- SKI (signal-based monitoring, Roberts et al.) -------------------
    def ski_watch(self, r, now):
        # readiness: the app logs "listening" when it is up
        gen = r.gen

        def ready_check(t):
            if gen != r.gen or r.container != "running":
                return
            if r.app:
                self.at(t + self.cfg.ski_latency, lambda t2: gen == r.gen and r.set_ready(t2, True))
            else:
                self.at(t + 0.05, ready_check)
        self.at(now + 0.05, ready_check)

    def ski_signal(self, now, r):
        """the wrapper shell logged the failure line"""
        cfg = self.cfg
        gen = r.gen

        def act(t):
            if gen != r.gen:
                return
            self.ski_events.append(t)
            r.set_ready(t, False)
            r.kill(t, "ski")   # kubelet kill with the 2 s grace period, as in the paper
        self.at(now + cfg.ski_latency, act)

    # ---- faults ---------------------------------------------------------------
    def inject_app_death(self, now):
        r = self.reps[0]
        r.app = False
        r.fail_all(now)
        if self.cfg.ski:
            self.ski_signal(now, r)

    def inject_hang(self, now):
        r = self.reps[0]
        if r.container != "running" or not r.app:
            # pick any running replica
            cands = [x for x in self.reps if x.container == "running" and x.app]
            if not cands:
                return
            r = cands[0]
        self.victim = r.idx
        r.advance(now)
        r.hung = True
        r.pending += self.cfg.workers      # hang goroutines count as pending
        r.ver += 1                          # cancel scheduled completions

    # ---- metrics ----------------------------------------------------------------
    def sample(self, now):
        self.samples.append((now - self.t_load,
                             sum(1 for r in self.reps if r.ready),
                             sum(1 for r in self.reps if r.app)))


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def pctl(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = max(0, int(math.ceil(p / 100 * len(xs))) - 1)
    return xs[i]


def summarize(sim: Sim) -> dict:
    cfg = sim.cfg
    rec = sim.records
    ok = [r for r in rec if r[2] == "o"]
    lat = [r[1] * 1000 for r in ok]
    n = cfg.n
    ready = [s[1] for s in sim.samples]
    t_load, t_end, t_inj = sim.t_load, sim.t_end, sim.t_inject
    restarts_window = sum(1 for r in sim.reps for t in r.start_times if t_load <= t <= t_end)
    kills_window = sum(1 for r in sim.reps for (t, _) in r.kill_times if t_load <= t <= t_end)
    out = dict(
        scenario=cfg.scenario, arm=cfg.arm, seed=cfg.seed, n=n,
        total=len(rec), ok=len(ok), failed=len(rec) - len(ok),
        timeouts=sum(1 for r in rec if r[2] == "t"),
        errors=sum(1 for r in rec if r[2] == "e"),
        success_rate=len(ok) / max(1, len(rec)),
        goodput=len(ok) / cfg.duration,
        p50_ms=pctl(lat, 50), p95_ms=pctl(lat, 95), p99_ms=pctl(lat, 99),
        availability=sum(ready) / (n * max(1, len(ready))),
        ready_sum=sum(ready),
        cascade_depth=n - min(ready) if ready else 0,
        restarts=restarts_window, kills=kills_window,
        gov_held_ticks=sim.gov.held_ticks if sim.gov else 0,
    )
    # per-second success ratio (for recovery / outage metrics)
    secs = int(cfg.duration) + 1
    sent = [0] * secs
    good = [0] * secs
    for t, _, o in rec:
        s = min(secs - 1, int(t))
        sent[s] += 1
        good[s] += o == "o"
    ratio = [good[i] / sent[i] if sent[i] else 1.0 for i in range(secs)]
    out["bad_seconds"] = sum(1 for x in ratio if x < 0.5)
    if cfg.scenario in ("R", "H"):
        # detection: first kill after injection (any replica in R, victim in H)
        victim = getattr(sim, "victim", 0)
        r = sim.reps[victim]
        kills = [t for (t, why) in r.kill_times if t >= t_inj - 1e-9]
        starts = [t for t in r.start_times if t >= t_inj]
        out["t_queue"] = (kills[0] - t_inj) if kills else float("nan")
        if cfg.ski and sim.ski_events:
            out["t_queue"] = sim.ski_events[0] - t_inj
        out["t_start"] = (starts[0] - t_inj) if starts else float("nan")
        out["detected"] = int(bool(kills))
        out["restarts_after_inject"] = len(starts)
    if cfg.scenario == "P" and cfg.burst:
        b_end = int(cfg.burst[0] + cfg.burst[1])
        rec_t = float("nan")
        for i in range(b_end, secs - 5):
            if all(ratio[j] >= 0.9 for j in range(i, i + 5)):
                rec_t = i - b_end
                break
        out["recovery_s"] = rec_t
        post = [r for r in rec if r[0] >= b_end]
        out["post_success"] = sum(1 for r in post if r[2] == "o") / max(1, len(post))
    if cfg.scenario == "D":
        s_end = int(cfg.inject_at + cfg.stall)
        rec_t = float("nan")
        for i in range(s_end, secs - 5):
            if all(ratio[j] >= 0.9 for j in range(i, i + 5)):
                rec_t = i - s_end
                break
        out["recovery_s"] = rec_t
        out["restarts_after_inject"] = sum(1 for r in sim.reps for t in r.start_times if t >= t_inj)
    out["ratio_series"] = ratio
    out["ready_series"] = ready
    return out


# ---------------------------------------------------------------------------
# arm presets (identical meaning to experiments/lib.sh)
# ---------------------------------------------------------------------------

READY_SUT = Probe("healthz", 5, 5, 6, 0)


def sut_cfg(scenario, arm, load_pct=100, seed=1, n=6, cap_per_replica=20.0,
            burst_pct=None, **kw) -> Cfg:
    live = None
    gov = None
    if arm == "DP":
        live = Probe("healthz", 3, 1, 3, 5)
    elif arm == "FP":
        live = Probe("healthz", 1, 1, 1, 5)
    elif arm == "ISO":
        live = Probe("naive", 1, 1, 1, 5)
    elif arm == "A":
        live = Probe("livez", 1, 1, 1, 5)
    elif arm == "B":
        gov = GovCfg(path="healthz")
    elif arm == "AB":
        gov = GovCfg(path="livez")
    elif arm.startswith("SWEEP"):
        # SWEEP:t:f:p
        _, t, f, p = arm.split(":")
        live = Probe("healthz", float(p), float(t), int(f), 5)
    elif arm != "NONE":
        raise ValueError(arm)
    if "gov" in kw:
        g = kw.pop("gov")
        if gov is not None:
            for k, v in g.items():
                setattr(gov, k, v)
    c = Cfg(scenario=scenario, arm=arm, seed=seed, n=n,
            rate=n * cap_per_replica * load_pct / 100.0,
            liveness=live, readiness=READY_SUT, governor=gov)
    if burst_pct is not None:
        c.burst = (60.0, 60.0, n * cap_per_replica * burst_pct / 100.0)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def repro_cfg(arm, seed=1, **kw) -> Cfg:
    """Roberts et al. setting: 1 catalogue pod, 100 users x 1 req/s, 0.5 s timeout."""
    common = dict(scenario="R", arm=arm, seed=seed, n=1, workers=10000, cpu=0.2,
                  work_cpu=0.0005, mode="closed", users=100, pace=1.0,
                  client_timeout=0.5, duration=290, inject_at=60,
                  start_min=0.8, start_max=2.2, restart_overhead=0.2)
    if arm == "DP":
        c = Cfg(**common, warmup=360,
                liveness=Probe("app", 3, 1, 3, 300, result_delay=0.2),
                readiness=Probe("app", 3, 1, 1, 180, result_delay=0.2))
        c.kill_grace = 30.0
    elif arm == "FP":
        c = Cfg(**common, warmup=360,
                liveness=Probe("app", 1, 1, 1, 1, result_delay=0.2),
                readiness=Probe("app", 1, 1, 1, 1, result_delay=0.2))
        c.kill_grace = 2.0
    elif arm == "SKI":
        c = Cfg(**common, warmup=360, ski=True,
                liveness=Probe("app", 3, 1, 3, 300, result_delay=0.2),
                readiness=None)
        c.kill_grace = 2.0
    else:
        raise ValueError(arm)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def simulate(cfg: Cfg) -> dict:
    return summarize(Sim(cfg).run())


if __name__ == "__main__":
    import json
    for arm in ["NONE", "FP", "A", "AB"]:
        r = simulate(sut_cfg("O", arm, 150, seed=1))
        r.pop("ratio_series"); r.pop("ready_series")
        print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}))
