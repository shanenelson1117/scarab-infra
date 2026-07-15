#!/usr/bin/env python3
"""Aggregate the branch-delaying-load reuse-distance study into a per-benchmark table
plus ONE overarching reuse-distance histogram.

Reads the per-simpoint `<bench>_<simpt>_reuse.csv` files emitted by Scarab's
shadow analyzer (src/bld_reuse.cc, enabled by `--branch_load_dep_reuse_study 1`)
and answers, per benchmark:

  * what fraction of branch-delaying-load lines are ever reused, and
  * how far apart the reuses are, as a STACK (reuse) distance = the number of
    DISTINCT lines touched between two consecutive accesses to the same line.

Two reference streams:
  full    -- distinct lines between ANY access i and access i+1 to a marked
             (feeder) line, over all demand accesses. This is the headline metric
             (the f50/f90/f95 percentiles and the overarching histogram) -> the
             line's depth in a shared L1/L2 LRU stack.
  feeder  -- distinct delaying-load lines between consecutive delaying-load
             accesses -> sizes a *dedicated* marked-load cache.

Each reuse is also split by the real-L1 outcome (hit vs miss) so we separate the
reuses today's cache already captures from those it evicts too early (the
opportunity a retention / dedicated-cache mechanism would target).

CSV row schema (per proc):
  <proc>,summary,<key>,<value>
  <proc>,hist,<stream>,<outcome>,<bucket>,<cache_size_lines>,<count>
    stream  in {full, feeder}
    outcome in {all, realhit, realmiss}
    bucket b -> a fully-associative cache of 2^b lines captures the reuse.

Usage:
  reuse_distance_summary.py [SIM_ROOT] [--config CFG] [--plot] [--csv OUT.csv]

  SIM_ROOT defaults to $REUSE_SIM_ROOT or /home/shanen/simulations/reuse_study.
  Per-simpoint CSVs are summed across simpoints; means are recomputed from the
  summed mass / reuse counts (access-weighted, not mean-of-means). f50/f90/f95 are
  bucket-resolution percentiles of the full-stream stack (reuse) distance.
"""
import sys, os, csv, glob, argparse
from collections import defaultdict

PROC = 0                        # single-thread memtrace runs use proc 0
PCTLS = (50, 90, 95, 99)
# The stream/outcome the headline percentiles + overarching histogram are built on.
HEADLINE = ("full", "all")


def parse_reuse_csv(path):
    """Return (summary: {key: float}, hist: {(stream,outcome): {bucket: count}})."""
    summary = {}
    hist = defaultdict(lambda: defaultdict(int))
    with open(path) as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#"):
                continue
            try:
                proc = int(row[0])
            except ValueError:
                continue
            if proc != PROC:
                continue
            kind = row[1]
            if kind == "summary" and len(row) >= 4:
                try:
                    summary[row[2]] = float(row[3])
                except ValueError:
                    pass
            elif kind == "hist" and len(row) >= 7:
                stream, outcome = row[2], row[3]
                try:
                    bucket = int(row[4])
                    count = int(row[6])
                except ValueError:
                    continue
                hist[(stream, outcome)][bucket] += count
    return summary, hist


def workload_of(path):
    """Results tree: .../<config>/<suite>/<workload>/<simpoint>/<bench>_<simpt>_reuse.csv"""
    parts = path.split(os.sep)
    if len(parts) >= 3:
        return parts[-3]
    return os.path.basename(path).rsplit("_", 1)[0].rsplit("_", 1)[0]


def pctiles_from_hist(buckets, targets=PCTLS):
    """Bucket-resolution percentiles: smallest 2^b (cache size, lines) at which the
    cumulative count reaches target%. Returns {target: value_or_None}, total."""
    out = {t: None for t in targets}
    total = sum(buckets.values())
    if not total:
        return out, total
    cum = 0
    for b in sorted(buckets):
        cum += buckets[b]
        frac = 100.0 * cum / total
        for t in targets:
            if out[t] is None and frac >= t:
                out[t] = 1 << b
    return out, total


class Agg:
    """Per-benchmark accumulator (summed across simpoints)."""

    def __init__(self):
        self.simpoints = 0
        self.delaying_pcs = 0                 # max seen (PC set is roughly stable)
        self.distinct_lines = 0
        self.reused_lines = 0
        self.delaying_accesses = 0
        self.real_hit = 0
        self.real_miss = 0
        self.feeder_reuse = 0
        self.feeder_dist_mass = 0.0
        self.full_reuse = 0
        self.full_dist_mass = 0.0
        self.hist = defaultdict(lambda: defaultdict(int))

    def add(self, summary, hist):
        self.simpoints += 1
        self.delaying_pcs = max(self.delaying_pcs, int(summary.get("delaying_pcs", 0)))
        self.distinct_lines += int(summary.get("distinct_delaying_lines", 0))
        self.reused_lines += int(summary.get("reused_delaying_lines", 0))
        self.delaying_accesses += int(summary.get("delaying_load_accesses", 0))
        self.real_hit += int(summary.get("real_hit", 0))
        self.real_miss += int(summary.get("real_miss", 0))
        fr = int(summary.get("feeder_reuse_recorded", 0))
        self.feeder_reuse += fr
        self.feeder_dist_mass += summary.get("feeder_mean_stack_dist", 0.0) * fr
        ur = int(summary.get("full_reuse_recorded", 0))
        self.full_reuse += ur
        self.full_dist_mass += summary.get("full_mean_stack_dist", 0.0) * ur
        for key, buckets in hist.items():
            for b, c in buckets.items():
                self.hist[key][b] += c

    @property
    def pct_reused(self):
        return 100.0 * self.reused_lines / self.distinct_lines if self.distinct_lines else 0.0

    @property
    def feeder_mean(self):
        return self.feeder_dist_mass / self.feeder_reuse if self.feeder_reuse else 0.0

    @property
    def full_mean(self):
        return self.full_dist_mass / self.full_reuse if self.full_reuse else 0.0

    @property
    def real_miss_share(self):
        tot = self.real_hit + self.real_miss
        return 100.0 * self.real_miss / tot if tot else 0.0


def human(n):
    if n is None:
        return ">cap"
    for unit, sz in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= sz:
            return f"{n // sz}{unit}"
    return str(n)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    default_root = os.environ.get("REUSE_SIM_ROOT", "/home/shanen/simulations/reuse_study")
    ap.add_argument("sim_root", nargs="?", default=default_root,
                    help=f"experiment results dir (default: {default_root})")
    ap.add_argument("--config", default=None,
                    help="restrict to a single configuration subdir (default: all)")
    ap.add_argument("--csv", default=None, help="also write the per-benchmark table to this CSV")
    ap.add_argument("--plot", action="store_true",
                    help="write the overarching stack reuse-distance histogram PNG")
    ap.add_argument("--plot-out", default=None,
                    help="path for the overarching histogram PNG (default: SIM_ROOT/reuse_hist.png)")
    args = ap.parse_args()

    root = args.sim_root
    sub = os.path.join(root, args.config) if args.config else root
    files = sorted(glob.glob(os.path.join(sub, "**", "*_reuse.csv"), recursive=True))
    if not files:
        print(f"no *_reuse.csv found under {sub}", file=sys.stderr)
        print("  (has the experiment finished? default output is per-simpoint "
              "<bench>_<simpt>_reuse.csv)", file=sys.stderr)
        return 1

    aggs = defaultdict(Agg)
    overall = defaultdict(int)            # the ONE overarching histogram (headline stream)
    n_parsed = 0
    for path in files:
        summary, hist = parse_reuse_csv(path)
        if not summary:
            continue
        aggs[workload_of(path)].add(summary, hist)
        for b, c in hist.get(HEADLINE, {}).items():
            overall[b] += c
        n_parsed += 1

    stream, outcome = HEADLINE
    print(f"# parsed {n_parsed} simpoint CSV(s) across {len(aggs)} benchmark(s) under {root}")
    print(f"# f50/f90/f95 = stack reuse distance ({stream} stream): DISTINCT lines "
          f"between any access i and access i+1 to a marked line (= fully-assoc cache "
          f"lines capturing that % of reuses)\n")

    hdr = (f"{'benchmark':<16} {'simpts':>6} {'PCs':>6} {'distinct':>10} "
           f"{'%reused':>8} {'miss%':>6} {'full_mean':>10} {'fdr_mean':>9} "
           f"{'f50':>7} {'f90':>7} {'f95':>7} {'f99':>7}")
    print(hdr)
    print("-" * len(hdr))

    rows_out = []
    for bench in sorted(aggs):
        a = aggs[bench]
        p, _ = pctiles_from_hist(a.hist.get(HEADLINE, {}))
        print(f"{bench:<16} {a.simpoints:>6} {a.delaying_pcs:>6} {a.distinct_lines:>10,} "
              f"{a.pct_reused:>7.1f}% {a.real_miss_share:>5.1f}% "
              f"{a.full_mean:>10,.0f} {a.feeder_mean:>9,.0f} "
              f"{human(p[50]):>7} {human(p[90]):>7} {human(p[95]):>7} {human(p[99]):>7}")
        rows_out.append(dict(
            benchmark=bench, simpoints=a.simpoints, delaying_pcs=a.delaying_pcs,
            distinct_delaying_lines=a.distinct_lines, pct_reused=round(a.pct_reused, 3),
            real_miss_share_pct=round(a.real_miss_share, 3),
            full_mean_stack_dist=round(a.full_mean, 2),
            feeder_mean_stack_dist=round(a.feeder_mean, 2),
            full_cache_lines_50=p[50], full_cache_lines_90=p[90],
            full_cache_lines_95=p[95], full_cache_lines_99=p[99],
            full_reuses=a.full_reuse, feeder_reuses=a.feeder_reuse,
            real_hit=a.real_hit, real_miss=a.real_miss))

    # ---- the one overarching histogram (all benchmarks pooled) ----
    op, ototal = pctiles_from_hist(overall)
    print(f"\n=== overarching stack reuse-distance histogram "
          f"({stream}/{outcome}, all benchmarks pooled) ===")
    print(f"{'cache<=':>12} {'count':>16} {'pct':>7} {'cum%':>7}")
    cum = 0
    for b in sorted(overall):
        cum += overall[b]
        print(f"{human(1 << b):>12} {overall[b]:>16,} "
              f"{100.0*overall[b]/ototal:>6.2f}% {100.0*cum/ototal:>6.2f}%")
    print(f"total reuses = {ototal:,}   "
          f"f50={human(op[50])}  f90={human(op[90])}  f95={human(op[95])}  f99={human(op[99])} "
          f"(fully-assoc cache lines)")

    print("\nLegend: distinct = distinct branch-delaying-load lines; %reused = lines "
          "touched >=2x by a delaying load.")
    print("  miss% = share of delaying-load accesses that MISSED the real L1.")
    print("  full_mean/fdr_mean = mean STACK distance (distinct lines) for the full "
          "(any access to a marked line) / feeder-only (delaying-load) stream.")
    print("  f50..f99 = smallest fully-assoc cache (lines) capturing that % of "
          "full-stream reuses.")

    if args.csv:
        fields = list(rows_out[0].keys()) if rows_out else []
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows_out)
        print(f"\nwrote {args.csv}")

    if args.plot:
        plot_overall(overall, op, stream,
                     args.plot_out or os.path.join(root, "reuse_hist.png"))

    return 0


def plot_overall(overall, pctl, stream, out_path):
    """One overarching histogram: pooled stack reuse-distance distribution."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available; skipping --plot", file=sys.stderr)
        return
    if not overall:
        print("no histogram data to plot", file=sys.stderr)
        return
    bmin, bmax = min(overall), max(overall)
    buckets = list(range(bmin, bmax + 1))
    total = sum(overall.values())
    counts = [overall.get(b, 0) for b in buckets]
    labels = [human(1 << b) for b in buckets]

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.bar(range(len(buckets)), counts, color="#3182bd", width=0.85)
    ax.set_xticks(range(len(buckets)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("stack reuse distance — distinct lines between any access i and i+1 "
                  "to a marked line  (<= fully-assoc cache lines, log2 bucket)")
    ax.set_ylabel("reuses")
    ax.set_title("Branch-delaying-load reuse-distance histogram  [kmhv3, all benchmarks pooled]")
    ax.grid(axis="y", ls=":", alpha=0.5)

    # cumulative % on a twin axis + percentile markers
    ax2 = ax.twinx()
    cum, ys = 0, []
    for b in buckets:
        cum += overall.get(b, 0)
        ys.append(100.0 * cum / total)
    ax2.plot(range(len(buckets)), ys, "-o", color="#e6550d", markersize=3, label="cumulative %")
    ax2.set_ylabel("cumulative % of reuses", color="#e6550d")
    ax2.set_ylim(0, 100)
    for tgt in (50, 90, 95):
        v = pctl.get(tgt)
        if v is None:
            continue
        idx = (v.bit_length() - 1) - bmin
        if 0 <= idx < len(buckets):
            ax2.axvline(idx, ls="--", color="#888", lw=1)
            ax2.text(idx, tgt, f" f{tgt}={human(v)}", fontsize=8, color="#333", va="bottom")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
