#!/usr/bin/env python3
"""Evaluate the attribution-sourced feeder reprieve replacement sweep.

Reads the consolidated `collected_stats.csv` produced by `./sci --collect-stats`,
weight-averages each workload's simpoints (via the per-simpoint `Weight` row),
and prints a per-benchmark table of IPC gain vs. baseline with one column per
reprieve config (n=1, n=2, n=4). A geomean summary row aggregates across
benchmarks.

Usage:
  ./eval_bld_repl_attr.py [collected_stats.csv]
      [--baseline baseline] [--configs attr_n1 attr_n2 attr_n4]
      [--csv out.csv]
"""
import argparse
import math
import sys

import pandas as pd

# Column labels are "<config> <workload> <simpoint>"; metadata rows carry the
# per-column config/workload/weight. The first three CSV columns are metadata.
META_COLS = 3


def _row(df, name):
    """Return the single stats-row named `name` restricted to simpoint columns."""
    sel = df[df["stats"] == name]
    if sel.empty:
        sys.exit(f"ERROR: row '{name}' not found in CSV")
    return sel.iloc[0, META_COLS:]


def weighted_ipc(ipc_row, weight_row, cfg_row, wl_row, cfg, wl):
    """Weight-average IPC over the simpoints belonging to (cfg, wl)."""
    num = den = 0.0
    for col in ipc_row.index:
        if cfg_row[col] != cfg or wl_row[col] != wl:
            continue
        try:
            v = float(ipc_row[col])
            w = float(weight_row[col])
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or math.isnan(w):
            continue
        num += v * w
        den += w
    return num / den if den > 0 else float("nan")


def bench_name(workload):
    """Leaf workload name, e.g. 'spec2006/rate_int/perlbench_2' -> 'perlbench_2'."""
    return workload.rsplit("/", 1)[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?",
                    default="/home/shanen/simulations/bld_repl_attr/collected_stats.csv")
    ap.add_argument("--baseline", default="baseline")
    ap.add_argument("--configs", nargs="+", default=["attr_n1", "attr_n2", "attr_n4"])
    ap.add_argument("--stat", default="IPC", help="stat to compare (default IPC)")
    ap.add_argument("--csv-out", default=None, help="also write the table to this CSV")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, low_memory=False)

    stat_row = _row(df, args.stat)
    weight_row = _row(df, "Weight")
    cfg_row = _row(df, "Configuration")
    wl_row = _row(df, "Workload")

    all_cfgs = set(cfg_row.values)
    for c in [args.baseline] + args.configs:
        if c not in all_cfgs:
            sys.exit(f"ERROR: config '{c}' not present in CSV (have: {sorted(all_cfgs)})")

    # Workloads that have a baseline result, in first-seen order.
    workloads = []
    seen = set()
    for col in wl_row.index:
        if cfg_row[col] == args.baseline and wl_row[col] not in seen:
            seen.add(wl_row[col])
            workloads.append(wl_row[col])

    # short config labels for the header
    def label(c):
        return c.replace("attr_", "").replace("_", "=") if c.startswith("attr_") else c

    rows = []
    ratios = {c: [] for c in args.configs}  # for geomean
    for wl in workloads:
        base = weighted_ipc(stat_row, weight_row, cfg_row, wl_row, args.baseline, wl)
        rec = {"benchmark": bench_name(wl), f"{args.baseline} {args.stat}": base}
        for c in args.configs:
            val = weighted_ipc(stat_row, weight_row, cfg_row, wl_row, c, wl)
            if base and not math.isnan(base) and not math.isnan(val):
                gain = (val / base - 1.0) * 100.0
                ratios[c].append(val / base)
            else:
                gain = float("nan")
            rec[f"{label(c)} %"] = gain
        rows.append(rec)

    table = pd.DataFrame(rows)

    # geomean of per-benchmark IPC ratios -> overall % gain
    geo = {"benchmark": "GEOMEAN", f"{args.baseline} {args.stat}": float("nan")}
    for c in args.configs:
        r = [x for x in ratios[c] if x > 0 and not math.isnan(x)]
        geo[f"{label(c)} %"] = (math.exp(sum(map(math.log, r)) / len(r)) - 1.0) * 100.0 if r else float("nan")
    table = pd.concat([table, pd.DataFrame([geo])], ignore_index=True)

    # pretty print
    pct_cols = [c for c in table.columns if c.endswith("%")]
    fmt = table.copy()
    fmt[f"{args.baseline} {args.stat}"] = fmt[f"{args.baseline} {args.stat}"].map(
        lambda v: f"{v:.4f}" if pd.notna(v) else "-")
    for c in pct_cols:
        fmt[c] = fmt[c].map(lambda v: f"{v:+.2f}" if pd.notna(v) else "-")
    print(f"{args.stat} gain vs '{args.baseline}' (weighted per workload)\n")
    print(fmt.to_string(index=False))

    if args.csv_out:
        table.to_csv(args.csv_out, index=False)
        print(f"\nwrote {args.csv_out}")


if __name__ == "__main__":
    main()
