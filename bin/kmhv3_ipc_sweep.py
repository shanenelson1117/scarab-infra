#!/usr/bin/env python3
"""§3.3 — Full-suite oracle IPC sweep on the kmhv3 replay data.

Per (config, workload) we aggregate across simpoints with instruction-throughput
weighting:  IPC = sum(NODE_INST_COUNT_total_count) / sum(NODE_CYCLE_total_count).
Reports IPC, % speedup vs baseline, suite average, and the "capture" ratio
(addr_100 IPC-gain / all_hit IPC-gain) for the branch-bound tier.
"""
import csv, sys
from collections import defaultdict

CSV = "/home/shanen/simulations/attr_replay_suite_kmhv3/collected_stats.csv"
CONFIGS = ["baseline", "all_hit", "addr_50", "addr_90", "addr_100"]
INST = "NODE_INST_COUNT_total_count"
CYC = "NODE_CYCLE_total_count"

# pull the two rows we need
need = {INST, CYC}
data = {}
with open(CSV) as f:
    reader = csv.reader(f)
    header = next(reader)
    for r in reader:
        if r and r[0] in need:
            data[r[0]] = r
            if len(data) == len(need):
                break
for k in need:
    if k not in data:
        sys.exit(f"missing stat {k}")

# workload -> config -> [inst, cyc]
agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
for idx, col in enumerate(header):
    parts = col.split()
    if len(parts) != 3:
        continue
    config, wl, _sp = parts
    if config not in CONFIGS:
        continue
    wl = wl.split("/")[-1]
    try:
        agg[wl][config][0] += float(data[INST][idx])
        agg[wl][config][1] += float(data[CYC][idx])
    except ValueError:
        pass

def ipc(wl, c):
    inst, cyc = agg[wl][c]
    return inst / cyc if cyc > 0 else float("nan")

rows = []
for wl in agg:
    if agg[wl]["baseline"][1] <= 0:
        continue
    b = ipc(wl, "baseline")
    rec = {"wl": wl, "baseline": b}
    for c in CONFIGS[1:]:
        v = ipc(wl, c)
        rec[c] = v
        rec[c + "_pct"] = 100.0 * (v - b) / b
    rows.append(rec)

rows.sort(key=lambda r: r["wl"])

hdr = f"{'workload':<22}{'baseline':>9}{'all_hit':>9}{'addr_50':>9}{'addr_90':>9}{'addr_100':>9}   {'all_hit%':>9}{'a50%':>8}{'a90%':>8}{'a100%':>8}"
print(hdr)
for r in rows:
    print(f"{r['wl']:<22}{r['baseline']:>9.4f}{r['all_hit']:>9.4f}{r['addr_50']:>9.4f}{r['addr_90']:>9.4f}{r['addr_100']:>9.4f}   "
          f"{r['all_hit_pct']:>8.2f}%{r['addr_50_pct']:>7.2f}%{r['addr_90_pct']:>7.2f}%{r['addr_100_pct']:>7.2f}%")

# suite averages (unweighted mean of per-workload IPC, matching report "Avg" row)
def avg(key):
    return sum(r[key] for r in rows) / len(rows)
print(f"\n{'AVG (mean IPC)':<22}{avg('baseline'):>9.4f}{avg('all_hit'):>9.4f}{avg('addr_50'):>9.4f}{avg('addr_90'):>9.4f}{avg('addr_100'):>9.4f}   "
      f"{avg('all_hit_pct'):>8.2f}%{avg('addr_50_pct'):>7.2f}%{avg('addr_90_pct'):>7.2f}%{avg('addr_100_pct'):>7.2f}%")

# capture table (addr_100 gain / all_hit gain), sorted by all_hit gain desc
print("\nCapture = addr_100 IPC-gain / all_hit IPC-gain:")
print(f"{'workload':<22}{'baseline':>9}{'addr_100%':>10}{'all_hit%':>10}{'capture':>9}")
cap_rows = [r for r in rows if r["all_hit_pct"] > 0.5]
cap_rows.sort(key=lambda r: r["addr_100_pct"] / r["all_hit_pct"] if r["all_hit_pct"] else 0, reverse=True)
for r in cap_rows:
    cap = 100.0 * r["addr_100_pct"] / r["all_hit_pct"]
    print(f"{r['wl']:<22}{r['baseline']:>9.3f}{r['addr_100_pct']:>9.2f}%{r['all_hit_pct']:>9.2f}%{cap:>8.0f}%")
