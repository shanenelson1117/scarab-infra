#!/usr/bin/env python3
"""§3.2 — Where mispredicted branches stall, on the kmhv3 attribution run.

Part 1 (bucket shares): from attr_suite_kmhv3 collected_stats.csv, sum the
BR_WAIT_* buckets suite-wide and report each as a share of
BR_EXEC_OPERAND_WAIT_MISPRED. Also report per-workload DCACHE_MISS share.

Part 2 (attribution category): parse the `#`-comment summary line of every
branch_delay_loads.csv (total_wait_cycles / ancestor / window_clog / none /
cold_cycles) and aggregate suite-wide into A/B/none/cold shares.
"""
import csv, glob, re
from collections import defaultdict

STATS = "/home/shanen/simulations/attr_suite_kmhv3/collected_stats.csv"
BDL_GLOB = "/home/shanen/simulations/attr_suite_kmhv3/baseline/spec2006/*/*/*/branch_delay_loads.csv"

BUCKETS = ["BR_WAIT_DCACHE_MISS", "BR_WAIT_LOAD_LATENCY", "BR_WAIT_EXEC_PORT",
           "BR_WAIT_ALU_LATENCY", "BR_WAIT_DCACHE_PORT", "BR_WAIT_MEM_BUFFER",
           "BR_WAIT_NOT_ISSUED", "BR_WAIT_UNRESOLVED"]
DENOM = "BR_EXEC_OPERAND_WAIT_MISPRED_total_count"

want = {b + "_total_count" for b in BUCKETS} | {DENOM}
rowmap = {}
with open(STATS) as f:
    reader = csv.reader(f)
    header = next(reader)
    for r in reader:
        if r and r[0] in want:
            rowmap[r[0]] = r

# suite totals + per-workload dcache share
def col_workload(col):
    parts = col.split()
    return parts[1].split("/")[-1] if len(parts) == 3 else None

suite_tot = defaultdict(float)
wl_dcache = defaultdict(float)
wl_denom = defaultdict(float)
for idx, col in enumerate(header):
    wl = col_workload(col)
    if wl is None or not col.startswith("baseline"):
        # attr_suite only has baseline config; guard anyway
        pass
    for stat, r in rowmap.items():
        try:
            v = float(r[idx])
        except (ValueError, IndexError):
            continue
        suite_tot[stat] += v
        if stat == "BR_WAIT_DCACHE_MISS_total_count" and wl:
            wl_dcache[wl] += v
        if stat == DENOM and wl:
            wl_denom[wl] += v

denom = suite_tot[DENOM]
print(f"BR_EXEC_OPERAND_WAIT_MISPRED total = {denom:,.0f} cycles\n")
print("Suite-wide BR_WAIT bucket shares:")
bsum = 0.0
for b in BUCKETS:
    v = suite_tot[b + "_total_count"]
    if v == 0:
        continue
    bsum += v
    print(f"  {b:<24}{v:>16,.0f}{100*v/denom:>8.1f}%")
print(f"  {'(sum of buckets)':<24}{bsum:>16,.0f}{100*bsum/denom:>8.1f}%")

print("\nTop DCACHE_MISS-share workloads (share of that workload's mispred operand-wait):")
shares = [(wl, wl_dcache[wl] / wl_denom[wl]) for wl in wl_denom if wl_denom[wl] > 1e5]
shares.sort(key=lambda x: x[1], reverse=True)
for wl, s in shares[:12]:
    print(f"  {wl:<16}{100*s:>6.1f}%   ({wl_denom[wl]:>14,.0f} cyc)")

# --- Part 2: attribution categories from branch_delay_loads.csv headers ---
pat = re.compile(r"total_wait_cycles=(\d+)\s+ancestor=(\d+)\s+window_clog=(\d+)\s+none=(\d+)\s+cold_cycles=(\d+)")
cat = defaultdict(int)
nfiles = 0
for path in glob.glob(BDL_GLOB):
    with open(path) as f:
        head = f.readline() + f.readline() + f.readline()
    m = pat.search(head)
    if not m:
        continue
    nfiles += 1
    tot, anc, win, non, cold = (int(x) for x in m.groups())
    cat["total"] += tot
    cat["ancestor"] += anc
    cat["window_clog"] += win
    cat["none"] += non
    cat["cold"] += cold

t = cat["total"]
print(f"\nAttribution categories (aggregated over {nfiles} branch_delay_loads.csv, "
      f"total_wait={t:,} cyc):")
if t:
    print(f"  A  ancestor (cone miss)    {100*cat['ancestor']/t:>6.1f}%")
    print(f"  B  window_clog             {100*cat['window_clog']/t:>6.1f}%")
    print(f"  none structural/non-mem    {100*cat['none']/t:>6.1f}%")
    print(f"  prefetchable (A+B)         {100*(cat['ancestor']+cat['window_clog'])/t:>6.1f}%")
    pf = cat['ancestor'] + cat['window_clog']
    if pf:
        print(f"  of prefetchable: cold      {100*cat['cold']/pf:>6.1f}%")
