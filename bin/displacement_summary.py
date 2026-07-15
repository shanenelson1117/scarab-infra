#!/usr/bin/env python3
"""Displacement-oracle summary: is a cache line spent on a feeder worth more IPC
than one spent on a count-matched reused non-feeder?

Compares three runs from the disp_replay experiment (per benchmark, instruction-
weighted across simpoints):
  baseline  -- no oracle.
  feeder    -- force-hit the smallest feeder-line set covering 90% of branch-stall
               cycles (N distinct lines = perfect feeder retention).
  nonfeeder -- force-hit N count-matched random reused-missed NON-feeder lines.

Reports feeder_gain% and nonfeeder_gain% over baseline and the DECISION delta
(feeder_gain - nonfeeder_gain): > 0 means feeder lines are worth more per line, so
shifting retention toward feeders has IPC headroom; ~0 or < 0 means a feeder-priority
replacement policy will wash out or hurt.

IPC = sum(NODE_INST_COUNT_total_count) / sum(NODE_CYCLE_total_count) over simpoints
(pattern from bin/kmhv3_ipc_sweep.py). Count-match is verified from the per-simpoint
replay_selection.csv markers (mode,target_N,selected_N).

Usage:
  displacement_summary.py [SIM_ROOT] [--csv OUT.csv] [--plot] [--plot-out P.png]
  SIM_ROOT defaults to $DISP_SIM_ROOT or ~/simulations/disp_replay.
"""
import sys, os, csv, glob, argparse
from collections import defaultdict

CONFIGS = ["baseline", "feeder", "nonfeeder"]
INST = "NODE_INST_COUNT_total_count"
CYC = "NODE_CYCLE_total_count"


def load_ipc(collected_csv):
    """Return agg[wl][config] = [inst, cyc] from collected_stats.csv."""
    need = {INST, CYC}
    data = {}
    with open(collected_csv) as f:
        reader = csv.reader(f)
        header = next(reader)
        for r in reader:
            if r and r[0] in need:
                data[r[0]] = r
                if len(data) == len(need):
                    break
    for k in need:
        if k not in data:
            sys.exit(f"missing stat {k} in {collected_csv}")
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
        except (ValueError, IndexError):
            pass
    return agg


def load_countmatch(sim_root):
    """Return match[wl] = (min_selected/target ratio, worst target, worst selected)
    from per-simpoint replay_selection.csv (last marker per mode per file)."""
    match = {}
    for cfg in ("feeder", "nonfeeder"):
        for path in glob.glob(os.path.join(sim_root, cfg, "**", "replay_selection.csv"),
                              recursive=True):
            wl = path.split(os.sep)[-3]
            last = {}
            with open(path) as f:
                for row in csv.reader(f):
                    if len(row) >= 3:
                        try:
                            last[row[0]] = (int(row[1]), int(row[2]))
                        except ValueError:
                            pass
            for mode, (tgt, sel) in last.items():
                cur = match.get(wl)
                ratio = sel / tgt if tgt else 1.0
                if cur is None or ratio < cur[0]:
                    match[wl] = (ratio, tgt, sel)
    return match


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    default_root = os.environ.get("DISP_SIM_ROOT",
                                  os.path.expanduser("~/simulations/disp_replay"))
    ap.add_argument("sim_root", nargs="?", default=default_root)
    ap.add_argument("--stats", default=None,
                    help="path to collected_stats.csv (default: SIM_ROOT/collected_stats.csv)")
    ap.add_argument("--csv", default=None, help="write the per-benchmark table to this CSV")
    ap.add_argument("--plot", action="store_true",
                    help="grouped bar chart of feeder vs non-feeder IPC gain per benchmark")
    ap.add_argument("--plot-out", default=None)
    args = ap.parse_args()

    stats = args.stats or os.path.join(args.sim_root, "collected_stats.csv")
    if not os.path.exists(stats):
        sys.exit(f"no collected_stats.csv at {stats} (run ./sci --collect-stats disp_replay)")
    agg = load_ipc(stats)
    match = load_countmatch(args.sim_root)

    def ipc(wl, c):
        inst, cyc = agg[wl][c]
        return inst / cyc if cyc > 0 else float("nan")

    rows = []
    for wl in sorted(agg):
        if agg[wl]["baseline"][1] <= 0:
            continue
        b = ipc(wl, "baseline")
        fed, nf = ipc(wl, "feeder"), ipc(wl, "nonfeeder")
        fg = 100.0 * (fed - b) / b if b > 0 else float("nan")
        ng = 100.0 * (nf - b) / b if b > 0 else float("nan")
        mr = match.get(wl)
        rows.append(dict(wl=wl, base=b, feeder=fed, nonfeeder=nf,
                         fgain=fg, ngain=ng, decision=fg - ng,
                         match=mr[0] if mr else None,
                         tgtN=mr[1] if mr else None, selN=mr[2] if mr else None))

    if not rows:
        sys.exit("no benchmarks with a valid baseline found")

    rows.sort(key=lambda r: r["decision"], reverse=True)
    hdr = (f"{'benchmark':<16} {'base':>7} {'feeder':>7} {'nonfdr':>7} "
           f"{'feed_g%':>8} {'nonf_g%':>8} {'DECISION':>9} {'N':>7} {'match%':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        matchpct = f"{100.0*r['match']:.0f}" if r["match"] is not None else "?"
        nstr = str(r["tgtN"]) if r["tgtN"] is not None else "?"
        print(f"{r['wl']:<16} {r['base']:>7.4f} {r['feeder']:>7.4f} {r['nonfeeder']:>7.4f} "
              f"{r['fgain']:>7.2f}% {r['ngain']:>7.2f}% {r['decision']:>+8.2f}% "
              f"{nstr:>7} {matchpct:>6}%")

    n = len(rows)
    mean = lambda k: sum(r[k] for r in rows) / n
    print("-" * len(hdr))
    print(f"{'MEAN':<16} {mean('base'):>7.4f} {mean('feeder'):>7.4f} {mean('nonfeeder'):>7.4f} "
          f"{mean('fgain'):>7.2f}% {mean('ngain'):>7.2f}% {mean('decision'):>+8.2f}%")

    under = [r["wl"] for r in rows if r["match"] is not None and r["match"] < 0.95]
    if under:
        print(f"\n[warn] count-match < 95% (non-feeder pool too small) for: {', '.join(under)}")
    print("\nDECISION = feeder_gain% - nonfeeder_gain%.  > 0: feeder lines are worth more "
          "per line -> feeder retention has IPC headroom.")
    print("  ~0 or < 0: a feeder-priority replacement policy will wash out or cost IPC.")
    print("  N = feeder/non-feeder lines retained (count-matched); match% = selected/target "
          "for the non-feeder set.")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv}")

    if args.plot:
        plot(rows, args.plot_out or os.path.join(args.sim_root, "displacement.png"))
    return 0


def plot(rows, out_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib/numpy not available; skipping --plot", file=sys.stderr)
        return
    labels = [r["wl"] for r in rows]
    x = np.arange(len(rows))
    fg = [r["fgain"] for r in rows]
    ng = [r["ngain"] for r in rows]
    w = 0.4
    fig, ax = plt.subplots(figsize=(max(8, 0.5 * len(rows) + 3), 6))
    ax.bar(x - w / 2, fg, w, label="feeder retention", color="#3182bd")
    ax.bar(x + w / 2, ng, w, label="non-feeder retention (count-matched)", color="#fdae6b")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("IPC gain over baseline (%)")
    ax.set_title("Displacement oracle: feeder vs count-matched non-feeder retention  [kmhv3]\n"
                 "(gap = per-line value advantage of feeders)")
    ax.grid(axis="y", ls=":", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
