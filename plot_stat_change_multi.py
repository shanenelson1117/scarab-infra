#!/usr/bin/env python3
"""
Multi-CSV "stat % change vs baseline" plotter -- an offshoot of plot_stat_change.py.

Same metric/aggregation/plotting logic as plot_stat_change.py, but:
  * accepts MULTIPLE collected_stats.csv paths, and
  * accepts an explicit list of configs to include (--configs), pulled from ANY of the CSVs.

This lets you assemble one comparison plot from configs that live in different CSV files
(e.g. a config from an L2 sweep CSV alongside a config from an FE sweep CSV), all shown as
% change vs a shared baseline config, per workload.

How the merge works:
  * Each config you name is taken from whichever CSV contains it. If the same config name
    appears in more than one CSV, the FIRST CSV (in the order given) wins and a warning is
    printed. The baseline config is resolved the same way.
  * (config, workload, simpoint) keys must line up across CSVs for the % change to be
    meaningful -- i.e. the CSVs should cover the same workloads/simpoints. pct_change matches
    each config's simpoints to the baseline's by (workload, simpoint) key, exactly as the
    original script does.

Stat selection (--stat, default "IPC"):
  * a raw row name (a bare NAME also tries the NAME_count / NAME_total_count suffixes),
  * the keyword IPC,
  * any ratio "A/B" of two rows.

MEASUREMENT WINDOW (--full-sim) -- differs from plot_stat_change.py:
  Scarab dumps every counter over two windows. By default this script reports the TARGET
  region only; --full-sim reports the FULL simulation (warmup + target).

                        default (target only)        --full-sim (warmup + target)
    IPC                 Periodic_Instructions        Cumulative_Instructions
                          / Periodic_Cycles            / Cumulative_Cycles
    bare NAME           NAME_count                   NAME_total_count
    scarab-infra name   "IPC"                        "IPC_total"

  These runs use --full_warmup (not --warmup), so WARMUP == 0 and the "ignore stats
  accumulated during warmup" reset in sim.c never fires -- the cumulative column really does
  include warmup. Prefer the default for policy comparisons: anything with a training or
  warm-up transient (a replacement predictor, a branch predictor) is flattered or penalised by
  the cumulative column according to how fast it converges rather than how good it is.

  NOTE: this INVERTS the previous behaviour. --stat IPC used to mean the cumulative figure,
  so plots made before this change correspond to today's --full-sim.

  An exactly-spelled row always wins over the suffix search, so --stat Cumulative_Cycles or
  --stat MLC_FILL_DIRTY_count is honoured verbatim whatever --full-sim says.

Aggregation (--agg, default "sum"): sum | geomean -- identical semantics to the original.

RAW VALUES (--raw):
  Plot the aggregated stat ITSELF instead of percent change vs the baseline.

  NO BASELINE IS REQUIRED. --baseline is only needed to compute a change against, so in raw
  mode it is optional: if the named one is absent the run proceeds with every config treated
  equally, and if it is present it is plotted like any other config (tinted grey in the chart,
  tagged "(baseline)" in the text output) rather than acting as the reference.

  Key sets also differ by mode. Percent mode can only score a simpoint the baseline also
  covers, so it intersects with the baseline's keys; raw mode has no such constraint and uses
  each config's own keys. With ragged coverage the two modes can therefore aggregate over
  different simpoints -- the printed n column shows this.

  Aggregation in raw mode:
    --agg sum      a plain row  -> sum of the cells; a ratio like IPC -> ratio of the summed
                   numerator to the summed denominator (NOT a mean of per-simpoint ratios)
    --agg geomean  geometric mean of the per-simpoint values

  Because geomean(a/b) == geomean(a)/geomean(b), the geomean raw values reproduce the geomean
  percentages exactly: e.g. raw 1.71584 vs 1.45514 is the same as the 17.92% reported by the
  default mode. Use --raw when the magnitudes matter (is 2% of an IPC of 0.3 or of 3.0?) and
  the default when comparing many configs against one reference.

PER-WORKLOAD BEST (--per-workload-best):
  Adds one extra bar to the overview. For each workload it adopts whichever config wins there,
  copies that winner's raw per-simpoint cells into a synthetic "best-per-workload" config, and
  aggregates it through the same pct_change() path as every other bar -- so it is directly
  comparable to them (averaging per-workload percentages instead would apply a different
  aggregation than the rest of the chart). The winners are printed, with a per-config win
  tally, since the bar is uninterpretable without them.

  This is an ORACLE: the winner is chosen using the same numbers being reported, so it is
  optimistically biased and no single static policy achieves it. Read it as an upper bound on
  what per-workload policy selection could buy -- e.g. the headroom a set-duelling or phase-
  adaptive scheme is chasing. The bar is hatched in the chart to keep that distinction visible.

  --best-configs A B C    restrict the oracle's candidate pool (default: every plotted config).
                          Configs named here are LOADED even if they are not in --configs, so
                          they can feed the oracle bar without appearing as bars of their own.
                          This is the profile-guided-tuning view: plot the fixed policies as
                          ordinary bars, and add ONE bar for "the best variant of the tunable
                          policy on each workload" rather than a bar per variant. e.g.

                            --configs baseline repl_srrip repl_ship mj_default \
                            --per-workload-best \
                            --best-configs depth_i48d32 depth_i72d48 depth_i96d64 instr_only_i96

                          plots four fixed policies plus one bar for a per-workload-tuned
                          marked-RRIP, which is what a PGO deployment could actually pick.
  --best-lower            lower % change wins (miss counts). Default: higher wins (IPC).
                          Getting this backwards silently selects the WORST config per
                          workload, so set it deliberately whenever --stat is not IPC.
  --best-include-baseline let the baseline win a workload, i.e. the oracle may decline to
                          change anything rather than being forced to adopt a config that
                          hurts there.

Usage:
    # reproduce the datacenter L2 by-workload IPC graph from one CSV (target region only):
    python plot_stat_change_multi.py td_complete_sweep_datacenter_l2.csv \
        --by-workload --agg geomean

    # same graph over the whole simulation, warmup included:
    python plot_stat_change_multi.py td_complete_sweep_datacenter_l2.csv \
        --by-workload --agg geomean --full-sim

    # cherry-pick a subset of configs (order controls the legend/bar order):
    python plot_stat_change_multi.py sweep_l2.csv \
        --configs rrip_min_neg8_noextrap rrip_min_neg64_extrap_anchor01 --by-workload

    # pull configs from several CSVs into one plot vs a shared baseline:
    python plot_stat_change_multi.py sweep_l2.csv sweep_fe.csv other.csv \
        --configs cfgA cfgB cfgC --baseline baseline --agg geomean --by-workload --out combined.png
"""

import argparse
import csv
import math
import os
import sys


# ------------------------------------------------------------------ shared helpers
# (identical to plot_stat_change.py so results match exactly)

def parse_label(label):
    """'<config> <suite>/<subsuite>/<workload> <simpoint>' -> (config, workload, simpoint)."""
    parts = label.strip().split()
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def read_table(csv_path):
    """Return (labels, rows): labels = data column headers, rows = {stat_name: [cells...]}."""
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = {r[0]: r[3:] for r in reader if r}
    return header[3:], rows


def series(labels, cells):
    """{(config, workload, simpoint): float} from one stat row, skipping bad/missing cells."""
    out = {}
    for i, label in enumerate(labels):
        key = parse_label(label)
        if key is None:
            continue
        try:
            v = float(cells[i])
        except (ValueError, IndexError):
            continue
        if math.isfinite(v):
            out[key] = v
    return out


def geomean(values):
    if not values:
        return float("nan")
    return math.exp(sum(math.log(v) for v in values) / len(values))


# ------------------------------------------------------------------ measurement window
#
# Every Scarab counter is dumped twice, once per window (statistics.c):
#   * "current interval"      -> Periodic_Cycles / Periodic_Instructions, and NAME_count
#   * "since start of run"    -> Cumulative_Cycles / Cumulative_Instructions, NAME_total_count
# These simpoint runs use --full_warmup (not --warmup), so WARMUP == 0 and the
# `reset_stats(FALSE)  // ignore stats accumulated during warmup` path in sim.c never fires.
# The cumulative column therefore spans WARMUP + TARGET, while the periodic column is the
# final dump interval, i.e. the target region alone.
#
# Default here is TARGET-ONLY. --full-sim switches to the cumulative column.
# (scarab-infra names the same two quantities "IPC" and "IPC_total" respectively.)

TARGET_IPC = ("Periodic_Instructions", "Periodic_Cycles")
FULLSIM_IPC = ("Cumulative_Instructions", "Cumulative_Cycles")


def window_name(full_sim):
    return "full sim (warmup+target)" if full_sim else "target only"


def resolve_row(rows, name, full_sim=False):
    """Match a stat name to an actual CSV row, trying the suffixes for the chosen window.

    An exact row name always wins, so an explicitly spelled row (e.g. 'MLC_FILL_DIRTY_count'
    or 'Cumulative_Cycles') is honoured verbatim regardless of --full-sim."""
    suffixes = ("_total_count", "_count") if full_sim else ("_count", "_total_count")
    for cand in (name,) + tuple(name + sfx for sfx in suffixes):
        if cand in rows:
            return cand
    return None


def resolve_metric(rows, stat, ctx="", full_sim=False):
    """Map a STAT spec to (num_row, den_row, pretty_name). den_row None => raw (denominator 1)."""
    s = stat.strip()
    if s.upper() == "IPC":
        num, den = FULLSIM_IPC if full_sim else TARGET_IPC
        if num not in rows or den not in rows:
            sys.exit(f"error: IPC over {window_name(full_sim)} needs '{num}' and '{den}' "
                     f"rows{ctx}")
        return num, den, f"IPC [{window_name(full_sim)}]"
    if "/" in s:
        a, b = (x.strip() for x in s.split("/", 1))
        ra, rb = resolve_row(rows, a, full_sim), resolve_row(rows, b, full_sim)
        if ra is None or rb is None:
            sys.exit(f"error: ratio stat '{s}'{ctx}: could not resolve "
                     f"{'numerator ' + a if ra is None else ''}"
                     f"{' and ' if ra is None and rb is None else ''}"
                     f"{'denominator ' + b if rb is None else ''}")
        return ra, rb, f"{ra}/{rb}"
    r = resolve_row(rows, s, full_sim)
    if r is None:
        sys.exit(f"error: stat '{s}' not found{ctx} (also tried _count/_total_count). "
                 f"Rows include e.g.: {', '.join(list(rows)[:6])} ...")
    return r, None, r


def configs_in_order(num):
    order = []
    for (config, _wl, _sp) in num:
        if config not in order:
            order.append(config)
    return order


def pct_change(num, den, base_num, base_den, config, workload, agg, raw=False):
    """Percent change of the metric for `config` (optionally within `workload`) vs baseline.

    With raw=True, return the AGGREGATED METRIC ITSELF instead of a change vs baseline --
    same aggregation, same simpoint set, just no division by the baseline. The baseline then
    has no special role and is plotted as an ordinary bar.

    Note the simpoint set is still intersected with the baseline's, so raw and percent modes
    cover exactly the same (workload, simpoint) keys and stay directly comparable."""
    # Percent mode can only score a simpoint the baseline also covers, so the key set is
    # intersected with it. Raw mode needs no baseline at all and uses the config's own keys
    # (base_num may legitimately be empty or None here).
    keys = [(wl, sp) for (c, wl, sp) in num
            if c == config and (workload is None or wl == workload)
            and (raw or (wl, sp) in base_num)]

    if raw:
        if agg == "geomean":
            vals = [v for (wl, sp) in keys
                    if (v := _metric_at(num, den, (config, wl, sp))) is not None and v > 0]
            if not vals:
                return float("nan"), 0
            return geomean(vals), len(vals)
        # sum aggregation: totals for a raw count, ratio-of-totals for a ratio metric
        cn = cd = 0.0
        used = 0
        for (wl, sp) in keys:
            cn += num[(config, wl, sp)]
            if den is not None:
                cd += den.get((config, wl, sp), 0.0)
            used += 1
        if used == 0:
            return float("nan"), 0
        if den is None:
            return cn, used
        return (cn / cd if cd else float("nan")), used

    if agg == "geomean":
        ratios = []
        for (wl, sp) in keys:
            vc = _metric_at(num, den, (config, wl, sp))
            vb = base_num[(wl, sp)] if den is None else (
                base_num[(wl, sp)] / base_den[(wl, sp)]
                if base_den.get((wl, sp)) else None)
            if vc is not None and vc > 0 and vb is not None and vb > 0:
                ratios.append(vc / vb)
        if not ratios:
            return float("nan"), 0
        return (geomean(ratios) - 1.0) * 100.0, len(ratios)

    # sum aggregation
    cn = bn = 0.0
    cd = bd = 0.0
    used = 0
    for (wl, sp) in keys:
        cn += num[(config, wl, sp)]
        bn += base_num[(wl, sp)]
        if den is not None:
            cd += den.get((config, wl, sp), 0.0)
            bd += base_den.get((wl, sp), 0.0)
        used += 1
    if used == 0:
        return float("nan"), 0
    if den is None:
        metric_c, metric_b = cn, bn
    else:
        if cd == 0 or bd == 0:
            return float("nan"), used
        metric_c, metric_b = cn / cd, bn / bd
    if metric_b == 0:
        return float("nan"), used
    return (metric_c / metric_b - 1.0) * 100.0, used


def _metric_at(num, den, key):
    a = num.get(key)
    if a is None:
        return None
    if den is None:
        return a
    b = den.get(key)
    if b is None or b == 0:
        return None
    return a / b


def all_workloads(num):
    return sorted({wl for (_c, wl, _sp) in num})


# ------------------------------------------------------------------ per-workload best (oracle)

BEST_LABEL = "best-per-workload"


def build_per_workload_best(candidates, baseline, num, den, base_num, base_den, agg,
                            lower_is_better=False, include_baseline=False, raw=False):
    """Add a synthetic config that, for each workload, adopts whichever CANDIDATE wins there.

    `candidates` is the pool the oracle may choose from -- by default every plotted config, or
    an explicit subset via --best-configs. Restricting the pool is the useful case: plot a set
    of fixed policies as ordinary bars, and draw ONE bar for "the best tuned variant of policy
    X on each workload", without cluttering the chart with every variant. That models a
    profile-guided deployment where the policy is tuned per application.

    This is an ORACLE: the winner is chosen using the very numbers being reported, so it is
    optimistically biased and is not achievable by any single static policy. It answers "how
    much is on the table if the policy were picked per workload", not "how good is policy X".

    Built by copying the winner's RAW per-simpoint cells into the synthetic config, so the
    resulting bar goes through exactly the same pct_change() aggregation as every other bar.
    Averaging per-workload percentages instead would silently use a different aggregation than
    the rest of the chart.

    Returns (num2, den2, winners, skipped) -- num2/den2 are copies with the synthetic config
    added, winners maps workload -> chosen config, skipped lists workloads with no usable
    config. If include_baseline, the baseline is an eligible choice (i.e. the oracle may
    decline to change anything for a workload, flooring that workload at ~0%)."""
    candidates = [c for c in candidates if c != baseline]
    if include_baseline:
        candidates = candidates + [baseline]
    if not candidates:
        return num, den, {}, []

    num2 = dict(num)
    den2 = dict(den) if den is not None else None
    winners, skipped = {}, []

    for wl in all_workloads(num):
        best_cfg, best_pct = None, None
        for c in candidates:
            pct, n = pct_change(num, den, base_num, base_den, c, wl, agg, raw=raw)
            if n == 0 or not math.isfinite(pct):
                continue
            if best_pct is None or (pct < best_pct if lower_is_better else pct > best_pct):
                best_cfg, best_pct = c, pct
        if best_cfg is None:
            skipped.append(wl)
            continue
        winners[wl] = best_cfg
        # adopt the winner's raw cells for this workload
        for (c, w, sp) in num:
            if c == best_cfg and w == wl:
                num2[(BEST_LABEL, w, sp)] = num[(c, w, sp)]
                if den2 is not None and (c, w, sp) in den:
                    den2[(BEST_LABEL, w, sp)] = den[(c, w, sp)]

    return num2, den2, winners, skipped


def print_per_workload_best(winners, skipped, lower_is_better, pool=None):
    direction = "lower is better" if lower_is_better else "higher is better"
    head = f"\nper-workload best ({direction}) -- ORACLE, selected on the reported data"
    print(head)
    print("-" * (len(head) - 1))
    if pool is not None:
        print(f"  pool ({len(pool)}): {', '.join(pool)}")
    for wl in sorted(winners):
        print(f"  {wl.split('/')[-1]:<34}{winners[wl]}")
    if skipped:
        print(f"  (no usable config for: {', '.join(w.split('/')[-1] for w in skipped)})")
    tally = {}
    for c in winners.values():
        tally[c] = tally.get(c, 0) + 1
    if tally:
        summary = ", ".join(f"{c} x{n}" for c, n in sorted(tally.items(), key=lambda kv: -kv[1]))
        print(f"  wins: {summary}")
    print()


# ------------------------------------------------------------------ multi-CSV merge (new)

def load_merged(csv_paths, stat, keep_configs, baseline, full_sim=False):
    """Merge several collected-stats CSVs into combined num/den series.

    keep_configs: iterable of config names to retain (plus baseline), or None to keep all.
    full_sim: select the cumulative (warmup+target) column instead of the target-only one.
    Returns (num, den_or_None, pretty, provenance) where provenance maps config -> csv path
    it was taken from. First-CSV-wins on duplicate (config, wl, sp) keys."""
    keep = None if keep_configs is None else (set(keep_configs) | {baseline})
    num = {}
    den = {}
    provenance = {}
    pretty = None
    has_den = None
    conflicts = 0

    for path in csv_paths:
        if not os.path.isfile(path):
            sys.exit(f"error: {path} not found.")
        labels, rows = read_table(path)
        num_row, den_row, p = resolve_metric(rows, stat, ctx=f" in {os.path.basename(path)}",
                                             full_sim=full_sim)
        if pretty is None:
            pretty = p
            has_den = den_row is not None
        elif (den_row is not None) != has_den:
            sys.exit(f"error: stat '{stat}' resolves with a denominator in some CSVs but not "
                     f"others (check {os.path.basename(path)}).")

        num_s = series(labels, rows[num_row])
        den_s = series(labels, rows[den_row]) if den_row else None

        for key, v in num_s.items():
            cfg = key[0]
            if keep is not None and cfg not in keep:
                continue
            if key in num:
                conflicts += 1
                continue  # first CSV wins
            num[key] = v
            provenance.setdefault(cfg, path)
        if den_s is not None:
            for key, v in den_s.items():
                if keep is not None and key[0] not in keep:
                    continue
                den.setdefault(key, v)

    if conflicts:
        print(f"warning: {conflicts} duplicate (config, workload, simpoint) cells across CSVs; "
              f"kept the first occurrence (earlier CSV wins).", file=sys.stderr)

    return num, (den if has_den else None), pretty, provenance


# ------------------------------------------------------------------ text output

def _fmt_cell(v, raw, width):
    """Right-aligned cell: raw values print with %g (magnitudes vary hugely across stats),
    percent change with a fixed 2dp and a trailing %."""
    if not math.isfinite(v):
        return f"{'--':>{width}}"
    return f"{v:>{width}.6g}" if raw else f"{v:>{width - 1}.2f}%"


def print_overall(order, baseline, num, den, base_num, base_den, agg, pretty, raw=False):
    # In raw mode the baseline is just another config, so it gets a row like everything else.
    hdr = (f"\n{pretty} (agg={agg})\n" if raw
           else f"\n% change in {pretty} vs '{baseline}' (agg={agg})\n")
    hdr += f"{'config':<32}{'n':>5}{'value' if raw else '% change':>12}"
    print(hdr)
    print("-" * (len(hdr.splitlines()[-1])))
    for config in order:
        if config == baseline and not raw:
            continue
        val, n = pct_change(num, den, base_num, base_den, config, None, agg, raw=raw)
        label = f"{config} (baseline)" if (raw and config == baseline) else config
        print(f"{label:<32}{n:>5}{_fmt_cell(val, raw, 12)}")
    print()


def print_by_workload(order, baseline, num, den, base_num, base_den, agg, pretty, raw=False):
    workloads = all_workloads(num)
    for config in order:
        if config == baseline and not raw:
            continue
        head = (f"\n[{config}]  {pretty} per workload (agg={agg})" if raw
                else f"\n[{config}]  % change in {pretty} vs '{baseline}' per workload (agg={agg})")
        print(head)
        print("-" * (len(head) - 1))
        for wl in workloads:
            val, n = pct_change(num, den, base_num, base_den, config, wl, agg, raw=raw)
            if n == 0:
                continue
            short = wl.split("/")[-1]
            print(f"  {short:<34}{_fmt_cell(val, raw, 9)}  (n={n})")
    print()


# ------------------------------------------------------------------ plotting

def _import_pyplot():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        print("(matplotlib not available; skipping chart. Use --no-plot to silence.)")
        return None


def _cmap(plt, n_cfg):
    # tab10 for <=10 configs (matches the original); tab20 when more so colors stay distinct.
    return plt.get_cmap("tab20" if n_cfg > 10 else "tab10"), (20 if n_cfg > 10 else 10)


def make_overall_plot(order, baseline, num, den, base_num, base_den, agg, pretty, out_path, raw=False):
    plt = _import_pyplot()
    if plt is None:
        return
    labels, vals = [], []
    for config in order:
        if config == baseline and not raw:  # raw: baseline is an ordinary bar
            continue
        val, n = pct_change(num, den, base_num, base_den, config, None, agg, raw=raw)
        if n == 0 or not math.isfinite(val):
            continue
        labels.append(config)
        vals.append(val)
    if not labels:
        print("(nothing to plot)")
        return

    fig, ax = plt.subplots(figsize=(max(6, 1.3 * len(labels)), 5))
    # percent mode colours by sign (gain vs regression); raw values are magnitudes, so a
    # single colour is used and the baseline bar is tinted to keep it identifiable.
    if raw:
        colors = ["#8c8c8c" if lb == baseline else "#4c78a8" for lb in labels]
    else:
        colors = ["#4c78a8" if v >= 0 else "#e45756" for v in vals]
    # hatch the oracle so it cannot be read as just another achievable policy
    hatches = ["//" if lb == BEST_LABEL else "" for lb in labels]
    bars = ax.bar(labels, vals, color=colors, hatch=hatches, edgecolor="#222", linewidth=0.6)
    if not raw:
        ax.axhline(0, color="#333", linewidth=0.8)
    ax.set_ylabel(pretty if raw else f"% change in {pretty} vs {baseline}")
    ax.set_title(f"{pretty} (agg={agg})" if raw
                 else f"{pretty}: % change vs {baseline} (agg={agg})")
    ax.tick_params(axis="x", rotation=30)
    for b, v in zip(bars, vals):
        ax.annotate(f"{v:.6g}" if raw else f"{v:.1f}%", (b.get_x() + b.get_width() / 2, v),
                    ha="center", va="bottom" if v >= 0 else "top", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"chart written to {out_path}")


def make_by_workload_plot(order, baseline, num, den, base_num, base_den, agg, pretty, out_path, raw=False):
    plt = _import_pyplot()
    if plt is None:
        return
    configs = [c for c in order if raw or c != baseline]  # raw: baseline is an ordinary series
    workloads = all_workloads(num)
    data = {c: {} for c in configs}
    for c in configs:
        for wl in workloads:
            val, n = pct_change(num, den, base_num, base_den, c, wl, agg, raw=raw)
            if n > 0 and math.isfinite(val):
                data[c][wl] = val
    workloads = [wl for wl in workloads if any(wl in data[c] for c in configs)]
    if not workloads or not configs:
        print("(nothing to plot per workload)")
        return

    short = [wl.split("/")[-1] for wl in workloads]
    n_wl, n_cfg = len(workloads), len(configs)
    x = list(range(n_wl))
    width = 0.8 / n_cfg
    cmap, ncol = _cmap(plt, n_cfg)

    fig, ax = plt.subplots(figsize=(max(10, 0.55 * n_wl), 6))
    for j, c in enumerate(configs):
        offs = [xi - 0.4 + width * (j + 0.5) for xi in x]
        vals = [data[c].get(wl, float("nan")) for wl in workloads]
        ax.bar(offs, vals, width=width, label=c, color=cmap(j % ncol))
    if not raw:
        ax.axhline(0, color="#333", linewidth=0.6)
    ax.set_ylabel(pretty if raw else f"% change in {pretty} vs {baseline}")
    ax.set_title(f"{pretty}: per-workload value (agg={agg})" if raw
                 else f"{pretty}: per-workload % change vs {baseline} (agg={agg})")
    ax.set_xticks(x)
    ax.set_xticklabels(short, rotation=90, fontsize=8)
    # Legend outside the axes (to the right) so it never overlaps the bars or the rotated
    # x-labels; a single column stays readable, spilling to 2 columns only when there are many
    # configs. bbox_inches="tight" at save time grows the canvas to include the whole legend, so
    # it is never clipped regardless of config count / label length.
    ncol = 1 if n_cfg <= 18 else 2
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8, ncol=ncol,
              title="config", framealpha=0.9, borderaxespad=0.0)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"per-workload chart written to {out_path}")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description="Plot a stat's % change vs a baseline across configs pulled from one or "
                    "more collected_stats.csv files.")
    ap.add_argument("csv_paths", nargs="+",
                    help="one or more collected_stats.csv files (from ./sci --collect-stats)")
    ap.add_argument("--stat", default="IPC",
                    help="stat to plot: a row name, 'IPC', or 'A/B' ratio (default: IPC)")
    ap.add_argument("--full-sim", action="store_true",
                    help="measure over the FULL simulation (warmup + target): IPC becomes "
                         "Cumulative_Instructions/Cumulative_Cycles and bare stat names prefer "
                         "NAME_total_count. Default is TARGET ONLY (Periodic_* / NAME_count), "
                         "which excludes warmup. These runs use --full_warmup, so the "
                         "cumulative column is NOT warmup-corrected.")
    ap.add_argument("--configs", nargs="+", default=None,
                    help="configs to include, taken from any CSV (default: all found). "
                         "Order controls the legend/bar order.")
    ap.add_argument("--baseline", default="baseline", help="baseline config name (default: baseline)")
    ap.add_argument("--agg", choices=["sum", "geomean"], default="sum",
                    help="cross-simpoint aggregation (default: sum)")
    ap.add_argument("--by-workload", action="store_true",
                    help="also break down / plot per workload (grouped bars)")
    ap.add_argument("--raw", action="store_true",
                    help="plot the AGGREGATED STAT VALUE itself instead of percent change vs "
                         "baseline. Same aggregation and same simpoint set, just no division "
                         "by the baseline -- so the baseline becomes an ordinary bar rather "
                         "than the reference. Use for absolute magnitudes (IPC, miss counts) "
                         "where the relative view hides how big the numbers actually are.")
    ap.add_argument("--per-workload-best", action="store_true",
                    help="add a 'best-per-workload' bar: for each workload adopt whichever "
                         "config wins there, then aggregate those winners like any other "
                         "config. This is an ORACLE (the winner is picked using the reported "
                         "numbers), so it upper-bounds what per-workload policy selection "
                         "could buy; no single static policy achieves it.")
    ap.add_argument("--best-configs", nargs="+", default=None,
                    help="with --per-workload-best, restrict the oracle's candidate pool to "
                         "these configs (default: every plotted config). Configs named here "
                         "are loaded from the CSVs even if they are not in --configs, so they "
                         "can feed the oracle bar WITHOUT appearing as bars of their own -- "
                         "e.g. plot the fixed policies, and add one bar for the best tuned "
                         "marked-RRIP variant per workload.")
    ap.add_argument("--best-lower", action="store_true",
                    help="with --per-workload-best, treat a LOWER %% change as better (use for "
                         "miss counts). Default: higher is better (correct for IPC).")
    ap.add_argument("--best-include-baseline", action="store_true",
                    help="with --per-workload-best, let the baseline win a workload -- i.e. the "
                         "oracle may leave a workload unchanged, flooring it at ~0%% instead of "
                         "being forced to adopt a config that hurts.")
    ap.add_argument("--no-plot", action="store_true", help="text output only")
    ap.add_argument("--out", default=None, help="chart output path (default: next to the first CSV)")
    args = ap.parse_args()

    # Oracle-pool configs must be READ even when they are not plotted, so widen the load
    # filter; `order` below still comes from --configs alone, so they stay off the chart.
    load_configs = args.configs
    if load_configs is not None and args.best_configs:
        load_configs = list(dict.fromkeys(list(load_configs) + list(args.best_configs)))
    num, den, pretty, provenance = load_merged(args.csv_paths, args.stat, load_configs,
                                               args.baseline, full_sim=args.full_sim)

    # State the window explicitly -- the two differ by the whole warmup region and the
    # difference is otherwise invisible in the output.
    print(f"measurement window: {window_name(args.full_sim)}"
          f"{'' if args.full_sim else '  (pass --full-sim to include warmup)'}")
    print(f"resolved stat: {pretty}")

    # A baseline is only required to compute a change against. --raw needs none, so a missing
    # one is fine there -- it just means no config gets the "(baseline)" tag.
    if args.baseline not in {c for (c, _w, _s) in num}:
        if not args.raw:
            sys.exit(f"error: baseline '{args.baseline}' not found in any CSV. "
                     f"Configs available: {', '.join(sorted(configs_in_order(num)))}")
        if args.best_include_baseline:
            sys.exit(f"error: --best-include-baseline needs baseline '{args.baseline}', which is "
                     f"not in any CSV. Configs available: "
                     f"{', '.join(sorted(configs_in_order(num)))}")

    # config ordering: honour --configs order; otherwise discovery order across CSVs.
    if args.configs:
        present = {c for (c, _w, _s) in num}
        order = [c for c in args.configs if c in present]
        missing = [c for c in args.configs if c not in present]
        if missing:
            print(f"warning: requested configs not found in any CSV: {', '.join(missing)}",
                  file=sys.stderr)
        if not order:
            sys.exit("error: none of the requested --configs were found in any CSV.")
    else:
        order = configs_in_order(num)

    # provenance report (helps confirm which CSV each config came from)
    have_baseline = args.baseline in {c for (c, _w, _s) in num}
    print("configs (source CSV):")
    for c in order:
        if c == args.baseline and have_baseline:
            continue
        print(f"  {c:<32} <- {os.path.basename(provenance.get(c, '?'))}")
    if have_baseline:
        print(f"  {args.baseline + ' (baseline)':<32} <- "
              f"{os.path.basename(provenance.get(args.baseline, '?'))}")

    base_num = {(wl, sp): v for (c, wl, sp), v in num.items() if c == args.baseline}
    base_den = ({(wl, sp): v for (c, wl, sp), v in den.items() if c == args.baseline}
                if den is not None else None)

    if args.per_workload_best:
        present = {c for (c, _w, _s) in num}
        if BEST_LABEL in present:
            sys.exit(f"error: a real config is already named '{BEST_LABEL}'; rename it or drop "
                     f"--per-workload-best.")
        if args.best_configs:
            pool = [c for c in args.best_configs if c in present]
            missing = [c for c in args.best_configs if c not in present]
            if missing:
                print(f"warning: --best-configs not found in any CSV: {', '.join(missing)}",
                      file=sys.stderr)
            if not pool:
                sys.exit("error: none of the requested --best-configs were found in any CSV.")
        else:
            pool = [c for c in order if c != args.baseline]
        num, den, winners, skipped = build_per_workload_best(
            pool, args.baseline, num, den, base_num, base_den, args.agg,
            lower_is_better=args.best_lower, include_baseline=args.best_include_baseline,
            raw=args.raw)
        if winners:
            order = order + [BEST_LABEL]
            print_per_workload_best(winners, skipped, args.best_lower, pool)
        else:
            print("warning: --per-workload-best found no usable workload; bar omitted.",
                  file=sys.stderr)

    print_overall(order, args.baseline, num, den, base_num, base_den, args.agg, pretty,
                  raw=args.raw)
    if args.by_workload:
        print_by_workload(order, args.baseline, num, den, base_num, base_den, args.agg, pretty,
                          raw=args.raw)

    if not args.no_plot:
        base_dir = os.path.dirname(os.path.abspath(args.csv_paths[0]))
        # pretty carries the window annotation ("IPC [target only]"); keep the window in the
        # filename so the two variants never overwrite each other, but strip shell-hostile chars.
        safe = pretty.replace("/", "_over_")
        safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in safe).strip("_")
        while "__" in safe:
            safe = safe.replace("__", "_")
        out = args.out or os.path.join(base_dir, f"stat_change_multi_{safe}.png")
        make_overall_plot(order, args.baseline, num, den, base_num, base_den, args.agg, pretty, out,
                          raw=args.raw)
        if args.by_workload:
            root, ext = os.path.splitext(out)
            make_by_workload_plot(order, args.baseline, num, den, base_num, base_den,
                                  args.agg, pretty, f"{root}_by_workload{ext or '.png'}",
                                  raw=args.raw)


if __name__ == "__main__":
    main()
