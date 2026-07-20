#!/usr/bin/env python3
"""Tabulate the feeder-access stream profile (branch_load_dep_feeder_access_profile).

Reads the consolidated `collected_stats.csv` produced by `./sci --collect-stats`
for a profiling run and, per benchmark, weight-averages each workload's
simpoints (via the per-simpoint `Weight` row) for the four counters:

    DCACHE_FEEDER_ACCESS_TOTAL      every on-path demand-load access to a feeder line
    DCACHE_FEEDER_ACCESS_COLD       first-ever access to the line  (compulsory / prefetch-reachable)
    DCACHE_FEEDER_ACCESS_REUSE_HIT  line was resident               (already captured by LRU)
    DCACHE_FEEDER_ACCESS_REUSE_MISS line was seen before but evicted (retention-reachable)

and prints a table of counts plus the derived fractions. `cold%` bounds how much
of the feeder stream is unreachable by any retention policy; `reuse_miss%` is the
headroom a smarter replacement policy could recover.

Usage:
  ./eval_feeder_access.py [collected_stats.csv] [--config profile] [--csv-out out.csv]
"""
import argparse
import math
import sys

import pandas as pd

# Column labels are "<config> <workload> <simpoint>"; the first three CSV columns
# are metadata (stat name column + two leading meta columns).
META_COLS = 3

TOTAL = "DCACHE_FEEDER_ACCESS_TOTAL"
COLD = "DCACHE_FEEDER_ACCESS_COLD"
REUSE_HIT = "DCACHE_FEEDER_ACCESS_REUSE_HIT"
REUSE_MISS = "DCACHE_FEEDER_ACCESS_REUSE_MISS"


def _row(df, name):
    """Return the single stats-row named `name` restricted to simpoint columns.

    `./sci --collect-stats` suffixes raw Scarab counters with `_count` (per-simpoint)
    / `_total_count` (cumulative); consolidated rows (Weight, Configuration, ...)
    carry no suffix. Try the bare name first, then the per-simpoint `_count` form.
    """
    for cand in (name, f"{name}_count"):
        sel = df[df["stats"] == cand]
        if not sel.empty:
            return sel.iloc[0, META_COLS:]
    sys.exit(f"ERROR: row '{name}' (nor '{name}_count') found in CSV")


def weighted(stat_row, weight_row, cfg_row, wl_row, cfg, wl):
    """Weight-average one counter over the simpoints belonging to (cfg, wl)."""
    num = den = 0.0
    for col in stat_row.index:
        if cfg_row[col] != cfg or wl_row[col] != wl:
            continue
        try:
            v = float(stat_row[col])
            w = float(weight_row[col])
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or math.isnan(w):
            continue
        num += v * w
        den += w
    return num / den if den > 0 else float("nan")


def bench_name(workload):
    """Leaf workload name, e.g. 'spec2017/rate_int_v2/mcf_r' -> 'mcf_r'."""
    return workload.rsplit("/", 1)[-1]


def pct(part, whole):
    return (part / whole * 100.0) if whole else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?",
                    default="/home/shanen/simulations/bld_feeder_access/collected_stats.csv")
    ap.add_argument("--config", default="profile",
                    help="configuration name to tabulate (default 'profile')")
    ap.add_argument("--csv-out", default=None, help="also write the table to this CSV")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, low_memory=False)

    rows = {name: _row(df, name) for name in (TOTAL, COLD, REUSE_HIT, REUSE_MISS)}
    weight_row = _row(df, "Weight")
    cfg_row = _row(df, "Configuration")
    wl_row = _row(df, "Workload")

    all_cfgs = set(cfg_row.values)
    if args.config not in all_cfgs:
        sys.exit(f"ERROR: config '{args.config}' not present in CSV (have: {sorted(all_cfgs)})")

    # Workloads for this config, in first-seen order.
    workloads, seen = [], set()
    for col in wl_row.index:
        if cfg_row[col] == args.config and wl_row[col] not in seen:
            seen.add(wl_row[col])
            workloads.append(wl_row[col])

    table_rows = []
    agg = {TOTAL: 0.0, COLD: 0.0, REUSE_HIT: 0.0, REUSE_MISS: 0.0}
    for wl in workloads:
        vals = {n: weighted(rows[n], weight_row, cfg_row, wl_row, args.config, wl)
                for n in (TOTAL, COLD, REUSE_HIT, REUSE_MISS)}
        tot = vals[TOTAL]
        if not (tot and not math.isnan(tot)):
            continue
        for n in agg:
            if not math.isnan(vals[n]):
                agg[n] += vals[n]
        table_rows.append({
            "benchmark": bench_name(wl),
            "total": tot,
            "cold": vals[COLD],
            "cold%": pct(vals[COLD], tot),
            "reuse_hit": vals[REUSE_HIT],
            "reuse_miss": vals[REUSE_MISS],
            "reuse%": pct(vals[REUSE_HIT] + vals[REUSE_MISS], tot),
            "reuse_miss%": pct(vals[REUSE_MISS], tot),
        })

    if not table_rows:
        sys.exit(f"ERROR: no feeder-access data for config '{args.config}' "
                 "(was branch_load_dep_feeder_access_profile enabled and a braddr set present?)")

    # Aggregate row: percentages from summed counts (weighted-count totals).
    tot = agg[TOTAL]
    table_rows.append({
        "benchmark": "TOTAL",
        "total": tot,
        "cold": agg[COLD],
        "cold%": pct(agg[COLD], tot),
        "reuse_hit": agg[REUSE_HIT],
        "reuse_miss": agg[REUSE_MISS],
        "reuse%": pct(agg[REUSE_HIT] + agg[REUSE_MISS], tot),
        "reuse_miss%": pct(agg[REUSE_MISS], tot),
    })

    table = pd.DataFrame(table_rows)

    fmt = table.copy()
    for c in ("total", "cold", "reuse_hit", "reuse_miss"):
        fmt[c] = fmt[c].map(lambda v: f"{v:,.0f}" if pd.notna(v) else "-")
    for c in ("cold%", "reuse%", "reuse_miss%"):
        fmt[c] = fmt[c].map(lambda v: f"{v:.1f}" if pd.notna(v) else "-")
    print("Feeder-access stream profile "
          f"(config '{args.config}', weighted per workload)\n")
    print(fmt.to_string(index=False))

    if args.csv_out:
        table.to_csv(args.csv_out, index=False)
        print(f"\nwrote {args.csv_out}")


if __name__ == "__main__":
    main()
