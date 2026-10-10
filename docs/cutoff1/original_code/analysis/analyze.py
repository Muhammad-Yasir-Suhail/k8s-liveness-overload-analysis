#!/usr/bin/env python3
"""
analyze.py — Group G18

Turns a sweep directory into the three artifacts the paper needs:

  1. results.csv      one row per run, all metrics
  2. heatmap.png      THE figure — instability boundary in (timeout,
                      failureThreshold) space, one panel per load level
  3. stats.txt        treatment-vs-control comparisons, Mann-Whitney U

Usage:  python3 analysis/analyze.py results/sweep-YYYYMMDD-HHMMSS
Deps:   pip install pandas matplotlib numpy scipy
"""

import sys, json, os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import mannwhitneyu

TARGET_READY = 6  # fixed replica count


def load_run(d):
    """Extract one row of metrics from a single run directory."""
    try:
        meta = json.load(open(os.path.join(d, "meta.json")))
    except Exception:
        return None

    row = dict(meta)

    # --- k6 client-side metrics ---------------------------------------
    try:
        k6 = json.load(open(os.path.join(d, "k6.json")))["metrics"]
        lat = k6.get("sut_latency", {}).get("values", {})
        row["p95_ms"] = lat.get("p(95)", np.nan)
        row["p99_ms"] = lat.get("p(99)", np.nan)
        row["error_rate"] = k6.get("sut_errors", {}).get("values", {}).get("rate", np.nan)
        row["timeouts"] = k6.get("sut_timeouts", {}).get("values", {}).get("count", 0)
        row["http_5xx"] = k6.get("sut_5xx", {}).get("values", {}).get("count", 0)
    except Exception:
        row.update(p95_ms=np.nan, p99_ms=np.nan, error_rate=np.nan,
                   timeouts=0, http_5xx=0)

    # --- time series: restarts, availability, cascade depth ------------
    ts_path = os.path.join(d, "timeseries.csv")
    if os.path.exists(ts_path):
        ts = pd.read_csv(ts_path, names=["ts", "ready", "restarts"])
        ts = ts.dropna()
        row["restarts"] = int(ts["restarts"].max() - ts["restarts"].min())
        # Availability: time-weighted fraction of full replica capacity
        row["availability"] = float(ts["ready"].mean() / TARGET_READY)
        # Cascade depth: worst simultaneous shortfall
        row["cascade_depth"] = int(TARGET_READY - ts["ready"].min())
    else:
        row.update(restarts=0, availability=np.nan, cascade_depth=0)

    return row


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: analyze.py <sweep-dir>")
    sweep = sys.argv[1]

    rows = []
    for name in sorted(os.listdir(sweep)):
        p = os.path.join(sweep, name)
        if os.path.isdir(p):
            r = load_run(p)
            if r:
                rows.append(r)

    if not rows:
        sys.exit(f"no runs found in {sweep}")

    df = pd.DataFrame(rows)
    out_csv = os.path.join(sweep, "results.csv")
    df.to_csv(out_csv, index=False)
    print(f"[+] {len(df)} runs -> {out_csv}")

    treat = df[df.arm == "treat"]
    ctrl = df[df.arm == "control"]

    # ---------------- heatmap: THE figure -----------------------------
    loads = sorted(df.load_pct.unique())
    timeouts = sorted(treat.timeoutSeconds.unique())
    thresholds = sorted(treat.failureThreshold.unique())

    fig, axes = plt.subplots(1, len(loads), figsize=(5 * len(loads), 4.2))
    if len(loads) == 1:
        axes = [axes]

    for ax, load in zip(axes, loads):
        base = ctrl[ctrl.load_pct == load]["availability"].mean()
        grid = np.full((len(thresholds), len(timeouts)), np.nan)

        for i, f in enumerate(thresholds):
            for j, t in enumerate(timeouts):
                sel = treat[(treat.load_pct == load) &
                            (treat.timeoutSeconds == t) &
                            (treat.failureThreshold == f)]
                if len(sel):
                    # Delta vs control. NEGATIVE = probe made things worse.
                    grid[i, j] = sel["availability"].mean() - base

        im = ax.imshow(grid, cmap="RdYlGn", vmin=-0.5, vmax=0.1,
                       aspect="auto", origin="lower")
        ax.set_xticks(range(len(timeouts)))
        ax.set_xticklabels([f"{t}s" for t in timeouts])
        ax.set_yticks(range(len(thresholds)))
        ax.set_yticklabels(thresholds)
        ax.set_xlabel("timeoutSeconds")
        if ax is axes[0]:
            ax.set_ylabel("failureThreshold")
        ax.set_title(f"Load {load}% of saturation\n(control availability = {base:.2f})")

        for i in range(len(thresholds)):
            for j in range(len(timeouts)):
                if not np.isnan(grid[i, j]):
                    ax.text(j, i, f"{grid[i,j]:+.2f}", ha="center",
                            va="center", fontsize=9)

    fig.colorbar(im, ax=axes, label="Δ availability vs no-probe control")
    fig.suptitle("Liveness probe effect on availability. "
                 "Red = self-healing made it worse.", y=1.02, fontsize=12)
    out_png = os.path.join(sweep, "heatmap.png")
    fig.savefig(out_png, dpi=160, bbox_inches="tight")
    print(f"[+] heatmap -> {out_png}")

    # ---------------- statistics --------------------------------------
    lines = ["Treatment vs no-probe control (Mann-Whitney U, two-sided)",
             "Negative delta means the liveness probe REDUCED availability.",
             "=" * 72]
    harmful = 0
    for load in loads:
        c = ctrl[ctrl.load_pct == load]["availability"].dropna()
        lines.append(f"\n--- Load {load}% ---")
        for t in timeouts:
            for f in thresholds:
                s = treat[(treat.load_pct == load) &
                          (treat.timeoutSeconds == t) &
                          (treat.failureThreshold == f)]["availability"].dropna()
                if len(s) < 2 or len(c) < 2:
                    continue
                delta = s.mean() - c.mean()
                try:
                    _, p = mannwhitneyu(s, c, alternative="two-sided")
                except ValueError:
                    p = float("nan")
                flag = ""
                if delta < 0 and p < 0.05:
                    flag = "  <-- HARMFUL (significant)"
                    harmful += 1
                lines.append(f"  timeout={t}s threshold={f:<3} "
                             f"delta={delta:+.3f}  p={p:.4f}{flag}")

    lines.append("\n" + "=" * 72)
    lines.append(f"Configurations where the probe significantly HURT: {harmful}")
    lines.append("A non-zero count here is the paper's headline result.")

    out_txt = os.path.join(sweep, "stats.txt")
    open(out_txt, "w").write("\n".join(lines))
    print(f"[+] stats -> {out_txt}")
    print("\n".join(lines[-3:]))


if __name__ == "__main__":
    main()
