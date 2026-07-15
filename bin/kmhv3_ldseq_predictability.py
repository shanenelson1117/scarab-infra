#!/usr/bin/env python3
"""§3.5 — Address-predictability analysis of the branch-delaying loads (kmhv3).

Pairs the NEW kmhv3 branch_delay_loads.csv (which PCs delay + cycle weights)
with the committed-load address streams (<bench>_<simpt>_ldseq.bin).  The ldseq
stream is a deterministic function of the trace + simpoint window (single-thread
memtrace), so it is microarch-config-independent and reused from the Jul-7 dump.

Predictors (64B cacheline granularity, address-only -- memtrace has no data):
  stride  : 2-delta stride
  stream  : next-line / small positive delta in {1..4}
  markov  : per-PC order-1 last-outcome table (temporal)
  gmarkov : global order-1 table (address correlation)
  novel   : line never seen before (upper bound on history predictors)
Coverage is cycle-weighted by each PC's branch-delay cycles.
"""
import sys, os, csv, glob
from collections import defaultdict
import numpy as np

LINE = 64
TRACE_DIR = os.environ.get("LDSEQ_DIR", "/home/mgiordan/samsung/bld_traces")
SIM_ROOT = "/home/shanen/simulations/attr_dump_kmhv3/baseline/spec2006"
OUT_PNG = "/home/shanen/simulations/kmhv3_analysis/br_addr_predictability.png"


def load_delay_pcs(bench, simpt):
    hits = glob.glob(f"{SIM_ROOT}/*/{bench}/{simpt}/branch_delay_loads.csv")
    if not hits:
        return None
    pc_cyc = defaultdict(int)
    with open(hits[0]) as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#") or row[0] == "category":
                continue
            try:
                pc = int(row[1], 16)
                pc_cyc[pc] += int(row[3])
            except (ValueError, IndexError):
                continue
    return dict(pc_cyc)


def global_pass(d_pc, d_line):
    per = defaultdict(lambda: dict(n=0, stride=0, stream=0, pmk=0, gmk=0,
                                   union=0, novel=0, distinct=None,
                                   x_stride=0, x_stream=0, x_pmk=0, x_gmk=0))
    last1 = {}
    last2 = {}
    pmk = {}
    seen = defaultdict(set)
    gmk = {}
    gprev = None
    pcs = d_pc.tolist()
    lines = d_line.tolist()
    for pc, x in zip(pcs, lines):
        c = per[pc]
        c["n"] += 1
        l1 = last1.get(pc)
        l2 = last2.get(pc)
        h_stride = l1 is not None and l2 is not None and (x - l1) == (l1 - l2)
        h_stream = l1 is not None and 1 <= (x - l1) <= 4
        tbl = pmk.get(pc)
        h_pmk = tbl is not None and tbl.get(l1) == x
        h_gmk = gprev is not None and gmk.get(gprev) == x
        if h_stride:
            c["stride"] += 1
        if h_stream:
            c["stream"] += 1
        if h_pmk:
            c["pmk"] += 1
        if h_gmk:
            c["gmk"] += 1
        if h_stride or h_stream or h_pmk or h_gmk:
            c["union"] += 1
            if h_stride:
                c["x_stride"] += 1
            elif h_stream:
                c["x_stream"] += 1
            elif h_pmk:
                c["x_pmk"] += 1
            else:
                c["x_gmk"] += 1
        s = seen[pc]
        if x not in s:
            c["novel"] += 1
            s.add(x)
        if l1 is not None:
            pmk.setdefault(pc, {})[l1] = x
        if gprev is not None:
            gmk[gprev] = x
        last2[pc] = l1
        last1[pc] = x
        gprev = x
    for pc, c in per.items():
        c["distinct"] = len(seen[pc])
    return per


def analyze(bench, simpt):
    path = f"{TRACE_DIR}/{bench}_{simpt}_ldseq.bin"
    if not os.path.exists(path):
        print(f"  [skip] no {path}")
        return None
    pc_cyc = load_delay_pcs(bench, simpt)
    if not pc_cyc:
        print(f"  [skip] no branch_delay_loads.csv for {bench}/{simpt}")
        return None

    raw = np.fromfile(path, dtype=np.uint64)
    raw = raw[: (raw.size // 2) * 2].reshape(-1, 2)
    pcs, vas = raw[:, 0], raw[:, 1]
    lines_all = vas // LINE

    delay_set = np.array(sorted(pc_cyc), dtype=np.uint64)
    mask = np.isin(pcs, delay_set)
    d_pc, d_line = pcs[mask], lines_all[mask]
    total_loads = raw.shape[0]

    per = global_pass(d_pc, d_line)

    rows = []
    tot_cyc = sum(pc_cyc.values())
    wsum = defaultdict(float)
    for pc, c in per.items():
        n = c["n"]
        if n == 0:
            continue
        cyc = pc_cyc.get(pc, 0)
        r = dict(pc=pc, cyc=cyc, n=n, distinct=c["distinct"])
        for k in ("stride", "stream", "pmk", "gmk", "union", "novel",
                  "x_stride", "x_stream", "x_pmk", "x_gmk"):
            r[k] = c[k] / n
            wsum[k] += r[k] * cyc
        rows.append(r)

    rows.sort(key=lambda r: r["cyc"], reverse=True)
    print(f"\n=== {bench}/{simpt} ===  total_loads={total_loads:,}  "
          f"delaying_instances={d_line.size:,}  delaying_PCs={len(rows)}  "
          f"delay_cycles={tot_cyc:,}")
    print(f"{'pc':>14} {'delay_cyc':>11} {'instances':>10} {'distinct':>9} "
          f"{'stride':>7} {'stream':>7} {'p-mkv':>7} {'g-mkv':>7} "
          f"{'UNION':>7} {'novel':>7}")
    for r in rows[:10]:
        print(f"0x{r['pc']:>12x} {r['cyc']:>11,} {r['n']:>10,} {r['distinct']:>9,} "
              f"{r['stride']*100:>6.1f}% {r['stream']*100:>6.1f}% "
              f"{r['pmk']*100:>6.1f}% {r['gmk']*100:>6.1f}% "
              f"{r['union']*100:>6.1f}% {r['novel']*100:>6.1f}%")
    if tot_cyc > 0:
        print(f"{'CYCLE-WEIGHTED':>14} {tot_cyc:>11,} {d_line.size:>10,} {'':>9} "
              f"{wsum['stride']/tot_cyc*100:>6.1f}% {wsum['stream']/tot_cyc*100:>6.1f}% "
              f"{wsum['pmk']/tot_cyc*100:>6.1f}% {wsum['gmk']/tot_cyc*100:>6.1f}% "
              f"{wsum['union']/tot_cyc*100:>6.1f}% {wsum['novel']/tot_cyc*100:>6.1f}%")
    return dict(bench=bench, simpt=simpt, rows=rows, tot_cyc=tot_cyc, wsum=dict(wsum))


def plot(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    results = [r for r in results if r and r["tot_cyc"] > 0]
    if not results:
        print("no results to plot")
        return
    labels = [f"{r['bench']}\n{r['simpt']}" for r in results]
    x = np.arange(len(results))
    def frac(r, k):
        return 100 * r["wsum"].get(k, 0) / r["tot_cyc"]
    stride = [frac(r, "x_stride") for r in results]
    stream = [frac(r, "x_stream") for r in results]
    pmk = [frac(r, "x_pmk") for r in results]
    gmk = [frac(r, "x_gmk") for r in results]
    resid = [max(0.0, 100 - frac(r, "union")) for r in results]
    fig, ax = plt.subplots(figsize=(9, 6))
    b = np.zeros(len(results))
    for vals, lab, col in [
        (stride, "stride", "#31a354"),
        (stream, "stream/next-line", "#a1d99b"),
        (pmk, "per-PC markov (temporal)", "#3182bd"),
        (gmk, "global markov (addr-corr)", "#9ecae1"),
        (resid, "unpredictable (addr-only)", "#d9d9d9"),
    ]:
        ax.bar(x, vals, 0.6, bottom=b, label=lab, color=col)
        b = b + np.array(vals)
    ax.set_ylabel("branch-delay stall cycles (%)")
    ax.set_title("Address predictability of branch-delaying loads  [kmhv3]\n"
                 "(cycle-weighted, exclusive first-hit; memtrace = address-only)")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylim(0, 100); ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=3)
    ax.grid(axis="y", ls=":", alpha=0.5)
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=130, bbox_inches="tight")
    print(f"\nwrote {OUT_PNG}")


if __name__ == "__main__":
    targets = sys.argv[1:] or [
        "mcf_6765", "mcf_9617", "mcf_16445",
        "astar_8282", "astar_7022", "astar_34395",
    ]
    results = []
    for t in targets:
        bench, simpt = t.rsplit("_", 1)
        results.append(analyze(bench, simpt))
    plot(results)
