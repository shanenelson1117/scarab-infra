#!/usr/bin/env python3
"""Aggregate the branch-delaying-load reuse-distance study into a per-benchmark table.

Reads the per-simpoint `<bench>_<simpt>_reuse.csv` files emitted by Scarab's
shadow stack-distance analyzer (src/bld_reuse.cc, enabled by
`--branch_load_dep_reuse_study 1`) and answers the two questions the study was
built for, per benchmark:

  * what fraction of branch-delaying-load lines are ever reused, and
  * how far apart (in DISTINCT lines = fully-associative cache size) are the reuses,

reported for two reference streams:

  feeder  -- distinct branch-delaying-load lines between reuses  -> sizes a
             *dedicated* feeder cache.
  full    -- distinct lines of ALL on-path demand accesses between reuses -> the
             line's depth in the shared L1/L2 LRU stack (explains the real-L1
             hit/miss and sizes a *shared* approach).

Each reuse is also split by the real-L1 outcome (hit vs miss) so we separate the
reuses the current cache already captures from those it evicts too early (the
opportunity a retention/dedicated-cache mechanism would target).

CSV row schema (per proc):
  <proc>,summary,<key>,<value>
  <proc>,hist,<stream>,<outcome>,<bucket>,<cache_size_lines>,<count>
    stream  in {feeder, full}
    outcome in {all, realhit, realmiss}
    bucket b -> a fully-associative cache of 2^b lines captures a reuse whose
                stack distance falls in that bucket.

Usage:
  reuse_distance_summary.py [SIM_ROOT] [--config CFG] [--plot] [--csv OUT.csv]

  SIM_ROOT defaults to $REUSE_SIM_ROOT or /home/shanen/simulations/reuse_study.
  Per-simpoint CSVs are aggregated to per-benchmark rows (summed across
  simpoints; means are recomputed from the summed distance mass / reuse counts,
  i.e. access-weighted, not a mean-of-means).
"""
import sys, os, csv, glob, argparse
from collections import defaultdict

PROC = 0                 # single-thread memtrace runs use proc 0
CDF_TARGETS = (50, 90, 95, 99)


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
    base = os.path.basename(path)
    return base.rsplit("_", 1)[0].rsplit("_", 1)[0]


class Agg:
    """Per-benchmark accumulator (summed across simpoints)."""

    def __init__(self):
        self.simpoints = 0
        self.delaying_pcs = 0                # max seen (PC set is roughly stable)
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

    # -- derived metrics --
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

    def cdf_knees(self, stream="feeder", outcome="all", targets=CDF_TARGETS):
        """Smallest fully-assoc cache size (in lines) capturing >= target% of reuses.

        Bucket b captures reuses with stack distance < 2^b; cumulating buckets in
        ascending order gives the fraction captured by a cache of 2^b lines.
        """
        buckets = self.hist.get((stream, outcome), {})
        total = sum(buckets.values())
        knees = {t: None for t in targets}
        if not total:
            return knees, total
        cum = 0
        for b in sorted(buckets):
            cum += buckets[b]
            frac = 100.0 * cum / total
            for t in targets:
                if knees[t] is None and frac >= t:
                    knees[t] = 1 << b
        return knees, total


def human_lines(n):
    if n is None:
        return ">cap"
    if n >= 1 << 20:
        return f"{n >> 20}M"
    if n >= 1 << 10:
        return f"{n >> 10}K"
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
    ap.add_argument("--plot", action="store_true", help="write per-benchmark feeder/full CDF PNGs")
    ap.add_argument("--plot-dir", default=None, help="directory for CDF PNGs (default: SIM_ROOT/reuse_plots)")
    args = ap.parse_args()

    root = args.sim_root
    if args.config:
        pattern = os.path.join(root, args.config, "**", "*_reuse.csv")
    else:
        pattern = os.path.join(root, "**", "*_reuse.csv")
    files = sorted(glob.glob(pattern, recursive=True))
    if not files:
        print(f"no *_reuse.csv found under {root}"
              f"{'/' + args.config if args.config else ''}", file=sys.stderr)
        print("  (has the experiment finished? default output is per-simpoint "
              "<bench>_<simpt>_reuse.csv)", file=sys.stderr)
        return 1

    aggs = defaultdict(Agg)
    n_parsed = 0
    for path in files:
        summary, hist = parse_reuse_csv(path)
        if not summary:
            continue
        aggs[workload_of(path)].add(summary, hist)
        n_parsed += 1

    print(f"# parsed {n_parsed} simpoint CSV(s) across {len(aggs)} benchmark(s) under {root}\n")

    hdr = (f"{'benchmark':<16} {'simpts':>6} {'PCs':>6} {'distinct':>10} "
           f"{'%reused':>8} {'feeder_d':>9} {'full_d':>9} {'miss%':>6} "
           f"{'f50':>6} {'f90':>6} {'f95':>6} {'full90':>7}")
    print(hdr)
    print("-" * len(hdr))

    rows_out = []
    for bench in sorted(aggs):
        a = aggs[bench]
        fknee, _ = a.cdf_knees("feeder", "all")
        uknee, _ = a.cdf_knees("full", "all")
        print(f"{bench:<16} {a.simpoints:>6} {a.delaying_pcs:>6} {a.distinct_lines:>10,} "
              f"{a.pct_reused:>7.1f}% {a.feeder_mean:>9,.0f} {a.full_mean:>9,.0f} "
              f"{a.real_miss_share:>5.1f}% "
              f"{human_lines(fknee[50]):>6} {human_lines(fknee[90]):>6} "
              f"{human_lines(fknee[95]):>6} {human_lines(uknee[90]):>7}")
        rows_out.append(dict(
            benchmark=bench, simpoints=a.simpoints, delaying_pcs=a.delaying_pcs,
            distinct_delaying_lines=a.distinct_lines, pct_reused=round(a.pct_reused, 3),
            feeder_mean_stack_dist=round(a.feeder_mean, 2),
            full_mean_stack_dist=round(a.full_mean, 2),
            real_miss_share_pct=round(a.real_miss_share, 3),
            feeder_reuses=a.feeder_reuse, full_reuses=a.full_reuse,
            real_hit=a.real_hit, real_miss=a.real_miss,
            feeder_cache_lines_50=fknee[50], feeder_cache_lines_90=fknee[90],
            feeder_cache_lines_95=fknee[95], feeder_cache_lines_99=fknee[99],
            full_cache_lines_90=uknee[90], full_cache_lines_95=uknee[95]))

    print("\nLegend: distinct = distinct branch-delaying-load lines; %reused = lines "
          "touched >=2x by a delaying load.")
    print("  feeder_d/full_d = access-weighted mean stack distance (distinct lines) "
          "for the feeder-only / full demand stream.")
    print("  miss% = share of delaying-load accesses that MISSED the real L1 (the "
          "reuses today's cache evicts too early).")
    print("  f50/f90/f95 = smallest fully-assoc *feeder* cache (lines) capturing that % "
          "of feeder reuses; full90 = same for the shared stream.")

    if args.csv:
        fields = list(rows_out[0].keys()) if rows_out else []
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows_out)
        print(f"\nwrote {args.csv}")

    if args.plot:
        plot_cdfs(aggs, args.plot_dir or os.path.join(root, "reuse_plots"))

    return 0


def plot_cdfs(aggs, out_dir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available; skipping --plot", file=sys.stderr)
        return
    os.makedirs(out_dir, exist_ok=True)

    def cdf_xy(hist_key, agg):
        buckets = agg.hist.get(hist_key, {})
        total = sum(buckets.values())
        if not total:
            return None, None
        xs, ys, cum = [], [], 0
        for b in sorted(buckets):
            cum += buckets[b]
            xs.append(1 << b)
            ys.append(100.0 * cum / total)
        return xs, ys

    for bench in sorted(aggs):
        a = aggs[bench]
        fig, ax = plt.subplots(figsize=(7, 5))
        plotted = False
        for key, lab, style in [
            (("feeder", "all"), "feeder (dedicated cache)", "-o"),
            (("full", "all"), "full stream (shared L1/L2)", "-s"),
            (("full", "realmiss"), "full, real-L1 miss (opportunity)", "--^"),
        ]:
            xs, ys = cdf_xy(key, a)
            if xs:
                ax.plot(xs, ys, style, label=lab, markersize=4)
                plotted = True
        if not plotted:
            plt.close(fig)
            continue
        ax.set_xscale("log", base=2)
        ax.set_xlabel("fully-associative cache size (lines)")
        ax.set_ylabel("% of reuses captured")
        ax.set_ylim(0, 100)
        ax.set_title(f"Branch-delaying-load reuse-distance CDF — {bench}  [kmhv3]")
        ax.grid(True, ls=":", alpha=0.5)
        ax.legend(loc="lower right")
        fig.tight_layout()
        out = os.path.join(out_dir, f"{bench}_reuse_cdf.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
