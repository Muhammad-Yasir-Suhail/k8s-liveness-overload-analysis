#!/usr/bin/env python3
"""
local_check.py -- 3-minute check of the core mechanism on REAL processes,
without Kubernetes (Objective O2: load -> health-check latency coupling).

It starts one service-under-test process, measures its capacity, then
offers 50 %, 90 % and 150 % of that capacity and records how long
/healthz (shared pool, the baseline) and /livez (Component A) take.
Finally it injects a silent hang and records when /livez turns to 503.

Two ways to run it:
  python3 experiments/local_check.py --docker      # uses image g18/tools:latest,
                                                  # CPU limited with --cpus (like a pod)
  python3 experiments/local_check.py --bin DIR     # uses Go binaries sut + loadgen in DIR
                                                  # (go build -o DIR ./src/cmd/...)

Writes results/local/local_check.json and report/generated/local_numbers.tex
"""
import argparse
import json
import os
import statistics as st
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results", "local")
GEN = os.path.join(ROOT, "report", "generated")
PORT = 18080


def run_loadgen(env, args):
    e = dict(os.environ, **{k: str(v) for k, v in env.items()})
    if args.docker:
        # load generator runs in its own container on a private network and
        # reaches the service by container name (works on Docker Desktop too)
        cmd = ["docker", "run", "--rm", "--network", "g18-local-net"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={str(v).replace('127.0.0.1', 'g18-local')}"]
        cmd += ["--entrypoint", "/loadgen", "g18/tools:latest"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # keep the load generator OFF the core that runs the service, otherwise
    # we would measure CPU contention between client and server
    n = os.cpu_count() or 1
    pre = ["taskset", "-c", f"1-{n-1}"] if n > 1 and _has_taskset() else []
    return subprocess.Popen(pre + [os.path.join(args.bin, "loadgen")], env=e, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)


def _has_taskset():
    return subprocess.run(["which", "taskset"], capture_output=True).returncode == 0


def summary(out):
    for line in out.splitlines():
        if line.startswith("SUMMARY "):
            return json.loads(line[8:])
    return {}


def probes(out):
    res = []
    for line in out.splitlines():
        if line.startswith("PROBE,"):
            _, t, url, lat, r = line.split(",")
            res.append(dict(t=int(t) / 1000, url=url, lat=float(lat), res=r))
    return res


def get(path, timeout=2):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docker", action="store_true")
    ap.add_argument("--bin", default="")
    ap.add_argument("--cpus", default="0.5", help="CPU limit (docker) -- like a pod limit")
    ap.add_argument("--work-units", default="100000")
    ap.add_argument("--gomaxprocs", default="8",
                    help="Go threads; in a pod Go 1.22 uses the host CPU count (8 on an HP 348)")
    args = ap.parse_args()
    if not args.docker and not args.bin:
        sys.exit("use --docker or --bin DIR")
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(GEN, exist_ok=True)
    env = dict(ROLE="sut", PORT=PORT, WORKERS=4, WORK_UNITS=args.work_units, LIVENESS_WINDOW_MS=5000)
    if args.docker:
        subprocess.run(["docker", "network", "create", "g18-local-net"], capture_output=True)
        cmd = ["docker", "run", "--rm", "-d", "--name", "g18-local", "--network", "g18-local-net",
               "-p", f"{PORT}:{PORT}", f"--cpus={args.cpus}"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        subprocess.run(["docker", "rm", "-f", "g18-local"], capture_output=True)
        subprocess.run(cmd + ["g18/tools:latest"], check=True, capture_output=True)
        stop = lambda: subprocess.run(["docker", "rm", "-f", "g18-local"], capture_output=True)  # noqa: E731
        mode = f"docker --cpus={args.cpus}"
    else:
        e = dict(os.environ, **{k: str(v) for k, v in env.items()}, GOMAXPROCS=args.gomaxprocs)
        pre = ["taskset", "-c", "0"] if subprocess.run(["which", "taskset"], capture_output=True).returncode == 0 else []
        proc = subprocess.Popen(pre + [os.path.join(args.bin, "sut")], env=e,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stop = proc.kill  # noqa: E731
        mode = f"native process pinned to 1 core, GOMAXPROCS={args.gomaxprocs}"
    try:
        for _ in range(50):
            if get("/livez") == 200:
                break
            time.sleep(0.2)
        target = f"http://127.0.0.1:{PORT}/work"
        # 1) capacity: closed loop, many users, no think time
        lg = run_loadgen(dict(TARGET=target, MODE="closed", USERS=32, PACE_MS=0, DURATION_S=8, TIMEOUT_MS=5000), args)
        cap = summary(lg.communicate()[0]).get("goodput_rps", 0)
        print(f"capacity: {cap:.1f} req/s ({mode})")
        result = dict(mode=mode, capacity=cap, loads={})
        # 2) coupling at 50/90/150 %
        for pct in (50, 90, 150):
            lg = run_loadgen(dict(TARGET=target, MODE="open", RATE=round(cap * pct / 100, 1),
                                  DURATION_S=15, TIMEOUT_MS=2000), args)
            time.sleep(3)
            pr = run_loadgen(dict(TARGET=f"http://127.0.0.1:{PORT}/livez", MODE="probe",
                                  PROBE_URLS=f"http://127.0.0.1:{PORT}/healthz,http://127.0.0.1:{PORT}/livez",
                                  DURATION_S=10, TIMEOUT_MS=1000), args)
            p = probes(pr.communicate()[0])
            s = summary(lg.communicate()[0])
            hz = [x for x in p if x["url"].endswith("/healthz")]
            lz = [x for x in p if x["url"].endswith("/livez")]
            d = dict(success=s.get("success_rate"),
                     healthz_med_ms=st.median([x["lat"] for x in hz]) if hz else None,
                     healthz_fail=sum(1 for x in hz if x["res"] != "200"), healthz_n=len(hz),
                     livez_med_ms=st.median([x["lat"] for x in lz]) if lz else None,
                     livez_fail=sum(1 for x in lz if x["res"] != "200"))
            result["loads"][pct] = d
            print(f"load {pct:3d}%: success {d['success']:.3f}  /healthz median {d['healthz_med_ms']:.1f} ms "
                  f"({d['healthz_fail']}/{d['healthz_n']} failed the 1 s timeout)  /livez median {d['livez_med_ms']:.2f} ms")
            time.sleep(3)
        # 3) silent hang
        lg = run_loadgen(dict(TARGET=target, MODE="open", RATE=round(cap * 0.5, 1), DURATION_S=14, TIMEOUT_MS=2000), args)
        time.sleep(2)
        t_h = time.time()
        get("/admin/hang")
        t_dead, naive_bad = None, 0
        while time.time() - t_h < 10:
            if t_dead is None and get("/livez", 1) == 503:
                t_dead = time.time() - t_h
            naive_bad += get("/livez-naive", 1) != 200
            time.sleep(0.25)
        lg.communicate()
        result["hang"] = dict(livez_dead_after_s=t_dead, naive_never_failed=(naive_bad == 0))
        print(f"hang: /livez -> 503 after {t_dead:.1f} s; /livez-naive never failed: {naive_bad == 0}")
    finally:
        stop()
    name = "local_check.json" if args.gomaxprocs != "1" else "local_check_gomaxprocs1.json"
    json.dump(result, open(os.path.join(OUT, name), "w"), indent=2)
    if args.gomaxprocs == "1":
        print("GOMAXPROCS=1 run saved separately (not used for the report macros)")
        return
    with open(os.path.join(GEN, "local_numbers.tex"), "w") as f:
        f.write("% generated by experiments/local_check.py\n")
        f.write(f"\\newcommand{{\\locMode}}{{{mode}}}\n\\newcommand{{\\locCap}}{{{cap:.0f}}}\n")
        for pct, name in ((50, "Fifty"), (90, "Ninety"), (150, "OneFifty")):
            d = result["loads"][pct]
            f.write(f"\\newcommand{{\\locHz{name}}}{{{d['healthz_med_ms']:.0f}}}\n")
            f.write(f"\\newcommand{{\\locLz{name}}}{{{d['livez_med_ms']:.1f}}}\n")
            f.write(f"\\newcommand{{\\locHzFail{name}}}{{{d['healthz_fail']}/{d['healthz_n']}}}\n")
        f.write(f"\\newcommand{{\\locHang}}{{{(result['hang']['livez_dead_after_s'] or float('nan')):.1f}}}\n")
    print("wrote results/local/local_check.json and report/generated/local_numbers.tex")


if __name__ == "__main__":
    main()
