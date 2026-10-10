#!/usr/bin/env python3
"""sampler.py NS SELECTOR CONTAINER OUT.csv [interval_s]

Every interval (default 1 s) records the state of the pods behind SELECTOR:
  t_unix, n_pods, n_ready, n_running, restarts_total, details
details = pod:ready:running:restarts:startedAt separated by '|'
Runs until killed. Uses only kubectl + the Python standard library."""
import json, subprocess, sys, time, signal

ns, sel, cname, out = sys.argv[1:5]
interval = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
stop = False
def _stop(*_):
    global stop
    stop = True
signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

with open(out, "a", buffering=1) as f:
    if f.tell() == 0:
        f.write("t_unix,n_pods,n_ready,n_running,restarts_total,details\n")
    nxt = time.time()
    while not stop:
        t = time.time()
        try:
            raw = subprocess.run(["kubectl", "get", "pods", "-n", ns, "-l", sel, "-o", "json"],
                                 capture_output=True, text=True, timeout=5).stdout
            items = json.loads(raw).get("items", [])
        except Exception:
            items = None
        if items is not None:
            n = ready = running = rs = 0
            det = []
            for p in items:
                if p["metadata"].get("deletionTimestamp"):
                    continue
                n += 1
                conds = {c["type"]: c["status"] for c in p.get("status", {}).get("conditions", [])}
                r = conds.get("Ready") == "True"
                cs = next((c for c in p.get("status", {}).get("containerStatuses", []) if c["name"] == cname), None)
                run = bool(cs and "running" in cs.get("state", {}))
                rc = cs.get("restartCount", 0) if cs else 0
                st = cs["state"]["running"].get("startedAt", "") if run else ""
                ready += r; running += run; rs += rc
                det.append(f'{p["metadata"]["name"]}:{int(r)}:{int(run)}:{rc}:{st}')
            f.write(f"{t:.3f},{n},{ready},{running},{rs},{'|'.join(det)}\n")
        nxt += interval
        time.sleep(max(0.0, nxt - time.time()))
