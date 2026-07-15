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
        plot_overlay(aggs, HEADLINE, args.plot_out or os.path.join(root, "reuse_hist.png"))

    return 0


def plot_overlay(aggs, headline, out_path):
    """Overarching CDF plot: one thin line per benchmark + the average in bold.

    Each line is the cumulative % of a benchmark's full-stream reuses captured by
    a fully-associative cache of the given size (i.e. the stack reuse-distance
    CDF). The bold line is the average across benchmarks (equal weight)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available; skipping --plot", file=sys.stderr)
        return

    all_buckets = sorted({b for a in aggs.values() for b in a.hist.get(headline, {})})
    if not all_buckets:
        print("no histogram data to plot", file=sys.stderr)
        return
    xs = list(range(all_buckets[0], all_buckets[-1] + 1))
    labels = [human(1 << b) for b in xs]

    def cdf(agg):
        h = agg.hist.get(headline, {})
        total = sum(h.values())
        if not total:
            return None
        ys, cum = [], 0
        for b in xs:
            cum += h.get(b, 0)
            ys.append(100.0 * cum / total)
        return ys

    curves = {name: c for name in sorted(aggs) for c in [cdf(aggs[name])] if c}
    if not curves:
        print("no per-benchmark curves to plot", file=sys.stderr)
        return
    # average across benchmarks (equal weight) at each bucket
    avg = [sum(curves[n][i] for n in curves) / len(curves) for i in range(len(xs))]

    fig, ax = plt.subplots(figsize=(11, 6))
    cmap = plt.get_cmap("tab20")
    for i, (name, ys) in enumerate(curves.items()):
        ax.plot(range(len(xs)), ys, "-", color=cmap(i % 20), lw=1.0, alpha=0.65, label=name)
    ax.plot(range(len(xs)), avg, "-", color="black", lw=3.0, label="AVERAGE (mean of benchmarks)")

    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("fully-associative cache size (lines) = stack reuse distance: distinct "
                  "lines between any access i and i+1 to a marked line")
    ax.set_ylabel("cumulative % of reuses captured")
    ax.set_ylim(0, 100)
    ax.set_title("Branch-delaying-load reuse-distance CDF  [kmhv3, per benchmark + average]")
    ax.grid(True, ls=":", alpha=0.5)

    # percentile markers read off the average curve
    for tgt in (50, 90, 95):
        idx = next((i for i, y in enumerate(avg) if y >= tgt), None)
        if idx is not None:
            ax.axhline(tgt, ls=":", color="#999", lw=0.8)
            ax.text(idx, tgt, f" f{tgt}={human(1 << xs[idx])}", fontsize=8, color="#333", va="bottom")

    n = len(curves) + 1                       # benchmarks + average
    ncol = min(8, max(3, -(-n // 3)))         # wide and short: aim for ~3 rows
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.34), ncol=ncol, fontsize=7,
              columnspacing=1.2, handlelength=1.4, borderaxespad=0.0, frameon=False)
    fig.subplots_adjust(bottom=0.30)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
