#!/usr/bin/env python3
"""§3.4 — Per-workload reduction in branch-exec operand-wait (mispred) stall
cycles, addr-replay coverage sweep vs the all_hit ceiling.  Rerun on the
kmhv3 data set.

Metric: BR_EXEC_OPERAND_WAIT_MISPRED_total_count. reduction% = 1 - config/base.
addr_100 should approach all_hit because the recorded braddr set IS the
branch-delaying-load set.
"""
import csv, sys
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

CSV = "/home/shanen/simulations/attr_replay_suite_kmhv3/collected_stats.csv"
OUT = "/home/shanen/simulations/kmhv3_analysis/br_stall_reduction.png"
STAT = "BR_EXEC_OPERAND_WAIT_MISPRED_total_count"
CONFIGS = ["baseline", "all_hit", "addr_50", "addr_90", "addr_100"]

with open(CSV) as f:
    reader = csv.reader(f)
    header = next(reader)
    row = None
    for r in reader:
        if r and r[0] == STAT:
            row = r
            break
if row is None:
    sys.exit(f"stat {STAT} not found")

agg = defaultdict(lambda: defaultdict(float))  # workload -> config -> cycles
for col, val in zip(header, row):
    parts = col.split()
    if len(parts) != 3:
        continue
    config, wl, _simpt = parts
    if config not in CONFIGS:
        continue
    try:
        agg[wl][config] += float(val)
    except ValueError:
        pass

rows = []
for wl, d in agg.items():
    base = d.get("baseline", 0.0)
    if base <= 0:
        continue
    rec = {"wl": wl.split("/")[-1], "base": base}
    for c in CONFIGS[1:]:
        rec[c] = 100.0 * (1 - d.get(c, base) / base)  # reduction %
    rows.append(rec)

rows.sort(key=lambda r: r["base"], reverse=True)

print(f"{'workload':<28}{'base_cyc':>14}  {'all_hit':>8}{'addr_50':>8}{'addr_90':>8}{'addr_100':>9}   capture(a100/allhit)")
for r in rows:
    cap = (r["addr_100"] / r["all_hit"] * 100) if r["all_hit"] > 0.5 else float("nan")
    print(f"{r['wl']:<28}{r['base']:>14,.0f}  {r['all_hit']:>7.1f}%{r['addr_50']:>7.1f}%{r['addr_90']:>7.1f}%{r['addr_100']:>8.1f}%   {cap:>6.0f}%")

tot = {c: sum(agg[w].get(c, agg[w]['baseline']) for w in agg if agg[w].get('baseline',0)>0) for c in CONFIGS}
print("\nSuite total branch-stall-cycle reduction vs baseline:")
for c in CONFIGS[1:]:
    print(f"  {c:<9} {100*(1-tot[c]/tot['baseline']):5.1f}%")

sig = [r for r in rows if r["all_hit"] >= 5.0]
sig.sort(key=lambda r: r["all_hit"], reverse=True)
labels = [r["wl"] for r in sig]
x = np.arange(len(labels))
w = 0.2
fig, ax = plt.subplots(figsize=(max(12, len(labels) * 0.5), 6.5))
ax.bar(x - 1.5*w, [r["addr_50"] for r in sig],  w, label="addr_50",  color="#c6dbef")
ax.bar(x - 0.5*w, [r["addr_90"] for r in sig],  w, label="addr_90",  color="#6baed6")
ax.bar(x + 0.5*w, [r["addr_100"] for r in sig], w, label="addr_100", color="#2171b5")
ax.bar(x + 1.5*w, [r["all_hit"] for r in sig],  w, label="all_hit (ceiling)", color="#525252")
ax.set_ylabel("Branch exec operand-wait (mispred) stall-cycle reduction  vs baseline (%)")
ax.set_title("Branch-delay-load prefetch: stall-cycle reduction vs perfect-D$ ceiling  [kmhv3]\n"
             "(spec2006 top-simpoints; workloads where all_hit reduces branch stall >=5%)")
ax.set_xticks(x); ax.set_xticklabels(labels, rotation=60, ha="right")
ax.axhline(0, color="k", lw=0.6); ax.legend(); ax.grid(axis="y", ls=":", alpha=0.5)
fig.tight_layout()
fig.savefig(OUT, dpi=130)
print(f"\nwrote {OUT}  ({len(sig)} workloads plotted)")
