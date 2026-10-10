#!/usr/bin/env python3
"""
analyze_testbed.py -- turn the raw Kubernetes runs into tables and figures.

    python3 analysis/analyze_testbed.py                 # results/testbed
    python3 analysis/analyze_testbed.py results/testbed-smoke

Reads   <results>/{R,calibration,O,P,H,D,S}/...
Writes  <results>/summary_<exp>.csv          one row per run
        report/generated/tb_*.tex            tables + numbers for the report
        report/figures/tb_*.pdf              figures

Only the Python standard library + matplotlib are needed.
Every metric is defined in the report (Sec. "Experimental Design").
"""
import csv
import glob
import json
import math
import os
import re
import statistics as st
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "results", "testbed")
if not os.path.isabs(RES):
    RES = os.path.abspath(RES)
# Only the real results (results/testbed) feed the report. Any other folder
# (e.g. results/testbed-smoke) is written to <folder>/preview so smoke-test
# numbers can never end up in the submitted PDF by accident.
if os.path.basename(os.path.normpath(RES)) == "testbed":
    GEN = os.path.join(ROOT, "report", "generated")
    FIG = os.path.join(ROOT, "report", "figures")
else:
    GEN = FIG = os.path.join(RES, "preview")
os.makedirs(GEN, exist_ok=True)
os.makedirs(FIG, exist_ok=True)

ARMS = ["NONE", "DP", "FP", "ISO", "A", "B", "AB"]
SHORT = {"NONE": "NONE", "DP": "DP", "FP": "FP", "ISO": "ISO", "A": "FP+A", "B": "FP+B", "AB": "FP+A+B"}
NAN = float("nan")


# ----------------------------------------------------------------- helpers
def isnan(x):
    return isinstance(x, float) and math.isnan(x)


def mean(xs):
    xs = [x for x in xs if x is not None and not isnan(x)]
    return st.mean(xs) if xs else NAN


def sd(xs):
    xs = [x for x in xs if x is not None and not isnan(x)]
    return st.pstdev(xs) if len(xs) > 1 else 0.0


def fmt(x, d=1):
    return "--" if x is None or isnan(x) else f"{x:.{d}f}"


def pm(xs, d=1):
    xs = [x for x in xs if x is not None and not isnan(x)]
    if not xs:
        return "--"
    return f"{fmt(mean(xs), d)} $\\pm$ {fmt(sd(xs), d)}" if len(xs) > 1 else fmt(xs[0], d)


def ts_rfc(s):
    if not s:
        return NAN
    return datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").timestamp()


GOLOG = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)")


def ts_golog(line):
    m = GOLOG.match(line)
    if not m:
        return NAN
    s = m.group(1)
    fmt_ = "%Y/%m/%d %H:%M:%S.%f" if "." in s else "%Y/%m/%d %H:%M:%S"
    return datetime.strptime(s, fmt_).replace(tzinfo=timezone.utc).timestamp()


def load_json(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def read_loadgen(path):
    """returns (start_ms, seconds[list of dict], summary dict)"""
    start, secs, summ = None, [], {}
    if not os.path.exists(path):
        return start, secs, summ
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("START "):
            start = int(line.split()[1])
        elif line.startswith("SEC,"):
            p = line.split(",")
            secs.append(dict(s=int(p[1]), sent=int(p[2]), ok=int(p[3]), to=int(p[4]), s5=int(p[5]), other=int(p[6])))
        elif line.startswith("SUMMARY "):
            summ = json.loads(line[8:])
    return start, secs, summ


def read_timeseries(path):
    rows = []
    if not os.path.exists(path):
        return rows
    # Files written during an interrupted run can contain NUL bytes;
    # the csv module rejects them, so strip them before parsing.
    with open(path, errors="replace") as f:
        clean_lines = (line.replace("\x00", "") for line in f)
        for r in csv.DictReader(clean_lines):
            try:
                det = []
                for d in (r.get("details") or "").split("|"):
                    if not d:
                        continue
                    name, ready, run, rc, started = d.split(":", 4)
                    det.append(dict(name=name, ready=int(ready), running=int(run), rc=int(rc), started=started))
                rows.append(dict(t=float(r["t_unix"]), n=int(r["n_pods"]), ready=int(r["n_ready"]),
                                 running=int(r["n_running"]), rs=int(r["restarts_total"]), det=det))
            except Exception:
                continue
    return rows


def at_second(ts, t0, s):
    """sample nearest to t0+s (ts sorted)"""
    target = t0 + s
    best = None
    for r in ts:
        if best is None or abs(r["t"] - target) < abs(best["t"] - target):
            best = r
        if r["t"] > target + 1:
            break
    return best


def window_samples(ts, t0, dur):
    return [r for r in ts if t0 <= r["t"] <= t0 + dur + 0.5]


def events(path):
    d = load_json(path, {}) or {}
    return d.get("items", [])


def ev_time(e):
    t = ts_rfc(e.get("firstTimestamp") or "")
    if isnan(t) and e.get("eventTime"):
        t = ts_rfc(e["eventTime"].split(".")[0] + "Z")
    return t


def liveness_kills(evs, pod=None, after=0.0):
    """times of 'failed liveness probe, will be restarted' (first/last ts of aggregated events)"""
    out = []
    for e in evs:
        if e.get("reason") != "Killing" or "liveness" not in (e.get("message") or ""):
            continue
        if pod and e.get("involvedObject", {}).get("name") != pod:
            continue
        ft, lt = ev_time(e), ts_rfc(e.get("lastTimestamp") or "")
        for t in (ft, lt):
            if not isnan(t) and t >= after - 1.0:
                out.append(t)
    return sorted(out)


def ratio_series(secs):
    return [(s["ok"] / s["sent"]) if s["sent"] else 1.0 for s in secs]


def recovery_after(ratio, start_s):
    for i in range(int(start_s), len(ratio) - 5):
        if all(ratio[j] >= 0.9 for j in range(i, i + 5)):
            return i - start_s
    return NAN


def write_csv(rows, name):
    if not rows:
        return
    keys = sorted({k for r in rows for k in r if not k.startswith("_")})
    with open(os.path.join(RES, f"summary_{name}.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


# ------------------------------------------------------------ Experiment R
def analyze_R():
    rows = []
    for d in sorted(glob.glob(os.path.join(RES, "R", "R_*"))):
        meta = load_json(os.path.join(d, "meta.json"))
        if not meta:
            continue
        start, secs, summ = read_loadgen(os.path.join(d, "loadgen.log"))
        ts = read_timeseries(os.path.join(d, "timeseries.csv"))
        evs = events(os.path.join(d, "events.json"))
        t0 = meta["t0_ms"] / 1000.0
        tin = meta["t_inject_ms"] / 1000.0
        arm = meta["arm"]
        r = dict(arm=arm, repeat=meta["repeat"], warmup_restarts=meta.get("warmup_restarts"))
        # time to queue restart
        if arm == "SKI":
            tq = NAN
            p = os.path.join(d, "ski.log")
            if os.path.exists(p):
                for line in open(p, errors="replace"):
                    if "SKI-DETECT" in line:
                        t = ts_golog(line)
                        if t >= tin - 0.5:
                            tq = t - tin
                            break
        else:
            k = liveness_kills(evs, after=tin)
            tq = (k[0] - tin) if k else NAN
        r["t_queue"] = tq
        # time to start restart: first container start after the injection
        starts = sorted({ts_rfc(x["started"]) for s in ts for x in s["det"] if x["started"]})
        later = [t for t in starts if t >= tin - 0.5]
        r["t_start"] = (later[0] - tin) if later else NAN
        # restarts after failure
        before = [s for s in ts if s["t"] <= tin]
        rc0 = before[-1]["rs"] if before else (ts[0]["rs"] if ts else 0)
        rc1 = ts[-1]["rs"] if ts else 0
        r["restarts_after_inject"] = rc1 - rc0
        # cumulative ready pods over the run (one sample per second, 0..duration)
        dur = int(meta.get("duration_s", 290))
        r["ready_sum"] = sum((at_second(ts, t0, s) or {"ready": 0})["ready"] for s in range(dur + 1)) if ts else NAN
        r["total"] = summ.get("total", NAN)
        r["ok"] = summ.get("ok", NAN)
        r["failed"] = summ.get("failed", NAN)
        r["http5xx"] = summ.get("http5xx", NAN)
        r["timeouts"] = summ.get("timeouts", NAN)
        # overhead: CPU (millicores) and memory (MiB) before the failure
        def cg(key0, key1):
            try:
                c0, _ = [float(x) for x in str(meta[key0]).split()]
                c1, m1 = [float(x) for x in str(meta[key1]).split()]
                dt = (meta["t_cg1_ms"] - meta["t_cg0_ms"]) / 1000.0
                return (c1 - c0) / 1e6 / dt * 1000.0, m1 / 2**20
            except Exception:
                return NAN, NAN
        r["cat_mcpu"], r["cat_mib"] = cg("cg_cat0", "cg_cat1")
        r["ski_mcpu"], r["ski_mib"] = cg("cg_ski0", "cg_ski1")
        rows.append(r)
    write_csv(rows, "R")
    return rows


# --------------------------------------------------------- Experiment C
def analyze_C():
    d = os.path.join(RES, "calibration")
    if not os.path.isdir(d):
        return None
    steps = []
    p = os.path.join(d, "steps.csv")
    if os.path.exists(p):
        with open(p) as f:
            for r in csv.DictReader(f):
                steps.append(dict(rate=float(r["rate"]), t0=int(r["t0_ms"]) / 1000.0, dur=float(r["duration_s"])))
    probes = []
    p = os.path.join(d, "prober.log")
    if os.path.exists(p):
        for line in open(p, errors="replace"):
            if line.startswith("PROBE,"):
                q = line.strip().split(",")
                probes.append(dict(t=int(q[1]) / 1000.0, url=q[2], lat=float(q[3]), res=q[4]))
    rows = []
    for s in steps:
        _, _, summ = read_loadgen(os.path.join(d, f"loadgen_rate{int(s['rate'])}.log"))
        inwin = [x for x in probes if s["t0"] + 5 <= x["t"] <= s["t0"] + s["dur"]]
        hz = [x["lat"] for x in inwin if x["url"].endswith("/healthz")]
        lz = [x["lat"] for x in inwin if x["url"].endswith("/livez")]
        hz_fail = sum(1 for x in inwin if x["url"].endswith("/healthz") and x["res"] != "200")
        rows.append(dict(rate=s["rate"], goodput=summ.get("goodput_rps", NAN), success=summ.get("success_rate", NAN),
                         p95=summ.get("p95_ms", NAN), healthz_med=st.median(hz) if hz else NAN,
                         healthz_max=max(hz) if hz else NAN, healthz_over1s=sum(1 for x in hz if x > 1000) / len(hz) if hz else NAN,
                         livez_med=st.median(lz) if lz else NAN, livez_max=max(lz) if lz else NAN, healthz_fail=hz_fail))
    write_csv(rows, "C")
    cap = NAN
    try:
        cap = float(open(os.path.join(d, "CAPACITY")).read().strip())
    except Exception:
        pass
    return rows, cap


# ------------------------------------------------ Experiments O, P, H, D, S
def analyze_sut(exp):
    rows = []
    for d in sorted(glob.glob(os.path.join(RES, exp, f"{exp}_*"))):
        meta = load_json(os.path.join(d, "meta.json"))
        if not meta:
            continue
        start, secs, summ = read_loadgen(os.path.join(d, "loadgen.log"))
        ts = read_timeseries(os.path.join(d, "timeseries.csv"))
        evs = events(os.path.join(d, "events.json"))
        t0 = meta["t0_ms"] / 1000.0
        dur = float(meta["duration_s"])
        n = int(meta["replicas"])
        r = dict(exp=exp, arm=meta["arm"], load_pct=meta["load_pct"], repeat=meta["repeat"],
                 offered_rate=meta["offered_rate"], probe_t=meta.get("probe_t"), probe_f=meta.get("probe_f"))
        r["success_rate"] = summ.get("success_rate", NAN)
        r["goodput"] = summ.get("goodput_rps", NAN)
        r["p95_ms"] = summ.get("p95_ms", NAN)
        r["p99_ms"] = summ.get("p99_ms", NAN)
        r["total"] = summ.get("total", NAN)
        win = window_samples(ts, t0, dur)
        if win:
            r["restarts"] = win[-1]["rs"] - win[0]["rs"]
            r["availability"] = mean([s["ready"] / n for s in win])
            r["cascade_depth"] = n - min(s["ready"] for s in win)
        else:
            r["restarts"] = r["availability"] = r["cascade_depth"] = NAN
        ratio = ratio_series(secs[:-1]) if secs else []
        r["bad_seconds"] = sum(1 for x in ratio if x < 0.5)
        r["_ratio"] = ratio
        r["liveness_kills"] = len(liveness_kills(evs, after=t0))
        gov = os.path.join(d, "governor.log")
        r["gov_restarts"] = r["gov_held_ticks"] = 0
        gov_restart_times = []
        if os.path.exists(gov):
            for line in open(gov, errors="replace"):
                if "GOV-RESTART pod=" in line:
                    r["gov_restarts"] += 1
                    m = re.search(r"pod=(\S+)", line)
                    gov_restart_times.append((ts_golog(line), m.group(1) if m else ""))
                if "GOV-TICK" in line and "held=true" in line:
                    r["gov_held_ticks"] += 1
            try:
                c = str(meta.get("governor_cg", "NA NA")).split()
                r["gov_cpu_s"] = float(c[0]) / 1e6
                r["gov_mib"] = float(c[1]) / 2**20
            except Exception:
                pass
        tin = meta.get("t_inject_ms", 0) / 1000.0
        if exp == "P":
            b_end = int(meta["inject_at_s"] + meta.get("burst_len_s", 60))
            r["recovery_s"] = recovery_after(ratio, b_end)
            post = [s for s in secs[:-1] if s["s"] >= b_end]
            r["post_success"] = sum(s["ok"] for s in post) / max(1, sum(s["sent"] for s in post))
        if exp == "H" and tin > 0:
            victim = meta.get("victim", "")
            if meta["arm"] in ("B", "AB"):
                tt = [t for t, p in gov_restart_times if p == victim and t >= tin - 0.5]
            else:
                tt = liveness_kills(evs, pod=victim, after=tin)
            r["t_queue"] = (tt[0] - tin) if tt else NAN
            r["detected"] = int(bool(tt))
            starts = sorted({ts_rfc(x["started"]) for s in ts for x in s["det"] if x["name"] == victim and x["started"]})
            later = [t for t in starts if t >= tin]
            r["t_start"] = (later[0] - tin) if later else NAN
        if exp == "D" and tin > 0:
            s_end = int(meta["inject_at_s"] + meta["stall_s"])
            r["recovery_s"] = recovery_after(ratio, s_end)
            before = [s for s in ts if s["t"] <= tin]
            rc0 = before[-1]["rs"] if before else 0
            r["restarts_after_inject"] = (ts[-1]["rs"] - rc0) if ts else NAN
        rows.append(r)
    write_csv(rows, exp)
    return rows


# ------------------------------------------------------------ write-out
def main():
    if not os.path.isdir(RES):
        sys.exit(f"no results in {RES} -- run the experiments first (docs/GUIDE.md)")
    print(f"reading {RES}")
    N = open(os.path.join(GEN, "tb_numbers.tex"), "w")
    N.write("% generated by analysis/analyze_testbed.py -- do not edit\n")
    N.write("\\makeatletter\n")

    def tb(arm, key, val):
        N.write(f"\\@namedef{{tb@{arm}@{key}}}{{{val}}}\n")

    def num(name, val):
        N.write(f"\\@namedef{{tbn@{name}}}{{{val}}}\n")

    env = os.path.join(RES, "environment.txt")
    if os.path.exists(env):
        lines = dict(l.split(":", 1) for l in open(env) if ":" in l)
        num("cpu", re.sub(r"([_%&#$])", r"\\\1", lines.get("cpu", "?").strip()))
        num("cpus", lines.get("cpus", "?").strip())
        try:
            num("memgb", f"{int(lines.get('mem_total_kb', '0'))/2**20:.1f}")
        except ValueError:
            pass
        num("os", lines.get("host_os", "?").strip().replace("_", "\\_"))
        num("docker", lines.get("docker", "?").strip())

    # ---- R
    R = analyze_R()
    if R:
        print(f"  R: {len(R)} runs")
        for arm in ("DP", "FP", "SKI"):
            rr = [r for r in R if r["arm"] == arm]
            if not rr:
                continue
            tb(arm, "q", pm([r["t_queue"] for r in rr]))
            tb(arm, "s", pm([r["t_start"] for r in rr]))
            tb(arm, "succ", fmt(mean([r["ok"] for r in rr]), 0))
            tb(arm, "rs", pm([r["restarts_after_inject"] for r in rr]))
            tb(arm, "ready", fmt(mean([r["ready_sum"] for r in rr]), 0))
            tb(arm, "n", len(rr))
            tb(arm, "catcpu", fmt(mean([r["cat_mcpu"] for r in rr]), 0))
            tb(arm, "catmib", fmt(mean([r["cat_mib"] for r in rr]), 1))
            tb(arm, "skicpu", fmt(mean([r["ski_mcpu"] for r in rr]), 0))
            tb(arm, "skimib", fmt(mean([r["ski_mib"] for r in rr]), 1))
            tb(arm, "warm", fmt(mean([float(r["warmup_restarts"] or 0) for r in rr]), 1))
        num("Rdone", "1")

    # ---- C
    C = analyze_C()
    if C and C[0]:
        rows, cap = C
        print(f"  C: {len(rows)} rate steps, capacity {cap}")
        top = max(rows, key=lambda r: r["rate"])
        if not isnan(top["goodput"]) and top["goodput"] >= 0.95 * top["rate"]:
            print("  WARNING: the highest calibration rate was still served -- capacity may be higher; "
                  "add larger rates to C_RATES in experiments/config.env and re-run 02_calibrate.sh")
        num("cap", fmt(cap, 1))
        num("Cdone", "1")
        with open(os.path.join(GEN, "tb_table_C.tex"), "w") as T:
            for r in sorted(rows, key=lambda x: x["rate"]):
                T.write(f"{r['rate']:.0f} & {fmt(r['goodput'])} & {fmt(100*r['success'] if not isnan(r['success']) else NAN)} & "
                        f"{fmt(r['p95'],0)} & {fmt(r['healthz_med'],0)} & {fmt(r['healthz_max'],0)} & {fmt(r['livez_med'],1)} \\\\\n")
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            rs = sorted(rows, key=lambda x: x["rate"])
            fig, ax = plt.subplots(figsize=(3.4, 2.2))
            x = [r["rate"] for r in rs]
            ax.plot(x, [r["healthz_med"] for r in rs], color="#eb6834", marker="s", lw=2, label="/healthz (shared pool)")
            ax.plot(x, [r["livez_med"] for r in rs], color="#2a78d6", marker="^", lw=2, label="/livez (Component A)")
            ax.axhline(1000, color="#8a8984", ls=":", lw=1)
            ax.text(x[0], 1100, "probe timeout 1 s", fontsize=7, color="#52514e")
            ax.set_yscale("symlog", linthresh=10)
            ax.set_xlabel("offered load, one replica (req/s)")
            ax.set_ylabel("median check latency (ms)")
            ax.spines[["top", "right"]].set_visible(False)
            ax.legend(fontsize=7, frameon=False)
            fig.savefig(os.path.join(FIG, "tb_C_coupling.pdf"), bbox_inches="tight")
            plt.close(fig)
        except Exception as e:  # matplotlib missing is not fatal
            print("  (figure skipped:", e, ")")

    # ---- O, P, H, D, S
    res = {}
    for exp in ("O", "P", "H", "D", "S"):
        rows = analyze_sut(exp)
        if rows:
            res[exp] = rows
            print(f"  {exp}: {len(rows)} runs")
            num(f"{exp}done", "1")
            num(f"{exp}runs", len(rows))

    if "O" in res:
        loads = sorted({r["load_pct"] for r in res["O"]})
        with open(os.path.join(GEN, "tb_table_O.tex"), "w") as T:
            for arm in ARMS:
                rr = [r for r in res["O"] if r["arm"] == arm]
                if not rr:
                    continue
                cells = []
                for l in loads:
                    x = [r for r in rr if r["load_pct"] == l]
                    cells += [fmt(100 * mean([r["success_rate"] for r in x]), 1), fmt(mean([r["restarts"] for r in x]), 1),
                              fmt(100 * mean([r["availability"] for r in x]), 0), fmt(mean([r["p99_ms"] for r in x]), 0)]
                T.write(f"{SHORT[arm]} & " + " & ".join(cells) + " \\\\\n")
        num("Oloads", ", ".join(f"{l}\\,\\%" for l in loads))
    if "P" in res:
        with open(os.path.join(GEN, "tb_table_P.tex"), "w") as T:
            for arm in ARMS:
                rr = [r for r in res["P"] if r["arm"] == arm]
                if not rr:
                    continue
                rec = [r["recovery_s"] for r in rr]
                nrec = sum(1 for x in rec if not isnan(x))
                rs = fmt(mean(rec), 0) if nrec else "never"
                if 0 < nrec < len(rr):
                    rs += f" ({nrec}/{len(rr)})"
                T.write(f"{SHORT[arm]} & {fmt(mean([r['restarts'] for r in rr]),1)} & {fmt(100*mean([r['availability'] for r in rr]),1)} & "
                        f"{fmt(100*mean([r['post_success'] for r in rr]),1)} & {rs} \\\\\n")
    if "H" in res:
        with open(os.path.join(GEN, "tb_table_H.tex"), "w") as T:
            for arm in ARMS:
                rr = [r for r in res["H"] if r["arm"] == arm]
                if not rr:
                    continue
                det = sum(r.get("detected", 0) for r in rr)
                q = [r["t_queue"] for r in rr if r.get("detected")]
                T.write(f"{SHORT[arm]} & {det}/{len(rr)} & {pm(q) if q else 'never'} & "
                        f"{pm([r['success_rate'] for r in rr], 3)} & {fmt(100*mean([r['availability'] for r in rr]),1)} \\\\\n")
    if "D" in res:
        with open(os.path.join(GEN, "tb_table_D.tex"), "w") as T:
            for arm in ARMS:
                rr = [r for r in res["D"] if r["arm"] == arm]
                if not rr:
                    continue
                rec = [r["recovery_s"] for r in rr]
                nrec = sum(1 for x in rec if not isnan(x))
                rs = fmt(mean(rec), 1) if nrec else "never"
                if 0 < nrec < len(rr):
                    rs += f" ({nrec}/{len(rr)})"
                T.write(f"{SHORT[arm]} & {pm([r['restarts_after_inject'] for r in rr])} & {pm([r['success_rate'] for r in rr], 3)} & "
                        f"{fmt(mean([r['bad_seconds'] for r in rr]),1)} & {rs} \\\\\n")
    # ablation (testbed)
    if all(k in res for k in ("O", "P", "H", "D")):
        with open(os.path.join(GEN, "tb_table_ablation.tex"), "w") as T:
            lmax = max(r["load_pct"] for r in res["O"] if r["load_pct"] <= 100) if any(r["load_pct"] <= 100 for r in res["O"]) else None
            for arm in ("FP", "A", "B", "AB"):
                o = [r for r in res["O"] if r["arm"] == arm and r["load_pct"] == lmax]
                p = [r for r in res["P"] if r["arm"] == arm]
                h = [r for r in res["H"] if r["arm"] == arm]
                d = [r for r in res["D"] if r["arm"] == arm]
                ca = "\\checkmark" if arm in ("A", "AB") else ""
                cb = "\\checkmark" if arm in ("B", "AB") else ""
                T.write(f"{SHORT[arm]} & {ca} & {cb} & {fmt(100*mean([r['success_rate'] for r in o]),1)} & "
                        f"{fmt(mean([r['restarts'] for r in o]),1)} & {fmt(100*mean([r['post_success'] for r in p]),1)} & "
                        f"{fmt(mean([r.get('t_queue', NAN) for r in h]),1)} & {fmt(mean([r.get('restarts_after_inject', NAN) for r in d]),1)} & "
                        f"{fmt(100*mean([r['success_rate'] for r in d]),1)} \\\\\n")
        num("ablationdone", "1")
    # governor overhead
    g = [r for e in res.values() for r in e if r["arm"] in ("B", "AB") and r.get("gov_cpu_s") is not None]
    if g:
        num("govcpu", fmt(mean([r["gov_cpu_s"] for r in g]), 2))
        num("govmib", fmt(mean([r["gov_mib"] for r in g]), 1))
    # timelines
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        col = {"NONE": "#52514e", "FP": "#eb6834", "A": "#2a78d6", "B": "#eda100", "AB": "#1baf7a"}
        for exp, span, label in (("P", None, "load spike"), ("D", None, "dependency stalled")):
            if exp not in res:
                continue
            fig, ax = plt.subplots(figsize=(7.0, 2.2))
            m0 = next(iter(res[exp]))
            for arm in ("NONE", "FP", "A", "B", "AB"):
                rr = [r for r in res[exp] if r["arm"] == arm and r["_ratio"]]
                if not rr:
                    continue
                n = min(len(r["_ratio"]) for r in rr)
                m = [100 * mean([r["_ratio"][i] for r in rr]) for i in range(n)]
                ax.plot(range(n), m, color=col[arm], lw=1.6, ls="--" if arm == "AB" else "-", label=SHORT[arm])
            ax.set_xlabel("time since load start (s)")
            ax.set_ylabel("successful requests (%)")
            ax.set_ylim(-3, 110)
            ax.spines[["top", "right"]].set_visible(False)
            ax.legend(fontsize=7, ncol=5, frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.22))
            fig.savefig(os.path.join(FIG, f"tb_{exp}_timeline.pdf"), bbox_inches="tight")
            plt.close(fig)
            num(f"{exp}fig", "1")
    except Exception as e:
        print("  (timeline figures skipped:", e, ")")
    N.write("\\makeatother\n")
    N.close()
    print(f"wrote {GEN}/tb_*.tex and {FIG}/tb_*.pdf -- now rebuild the report")


if __name__ == "__main__":
    main()
