# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

scarab-infra is the **orchestration layer** around the [Scarab](https://github.com/litz-lab/scarab) cycle-accurate microarchitecture simulator. It does **not** contain the simulator itself. It automates building Scarab, launching simulations across SimPoints inside Docker containers (locally or on Slurm), and collecting/analyzing the resulting statistics.

**The Scarab simulator source lives in a separate sibling repo**, typically `../scarab_sn` (here: `/home/mgiordan/samsung/scarab_sn`). Each descriptor points to it via `scarab_path`. When a task involves changing simulator behavior (pipeline stages, branch predictors, caches, `*.param.def`/`*.stat.def` files, `src/`), edit code in the `scarab_path` repo — **not** in scarab-infra. scarab-infra only builds, runs, and measures that code.

## The `sci` CLI

`./sci` is the single entry point — a ~4300-line standalone Python script (run with the `scarabinfra` conda env active). Almost every workflow is `./sci --<command> <descriptor>`, where `<descriptor>` is a filename (without `.json`) in `json/`.

Commands: `--init` / `--ci-init` (bootstrap env), `--build-scarab`, `--build-image`, `--list`, `--interactive`, `--trace`, `--sim`, `--perf`, `--visualize`, `--collect-stats`, `--collect-mem`, `--perf-analyze`, `--postprocess`, `--kill`, `--status`, `--clean`.

Typical loop: `./sci --build-scarab <d>` → `./sci --sim <d>` → `./sci --status <d>` → `./sci --collect-stats <d>` → `./sci --visualize <d>` / `--perf-analyze <d>`. There is no separate build/lint/test command for this repo — correctness is validated by running simulations end-to-end.

## Descriptors (`json/<name>.json`)

Descriptors are the central configuration object. `json/exp.json` is the annotated template (every field has a `_field` sibling key documenting it). Three `descriptor_type` values: `simulation`, `trace`, `perf`. Key fields:

- `root_dir` — mounted as the container home; outputs land in `<root_dir>/simulations/<experiment>/`.
- `scarab_path` — the Scarab source repo to build/run (the sibling repo above).
- `scarab_build` — `opt`, `opt-avx`, `dbg`, or null; must match a Scarab `src/Makefile` target. Use `dbg` for gdb work via `--interactive`.
- `workload_manager` — `manual` (run on local machine) or `slurm` (cluster scheduling). This selects the runner module.
- `simulations` — list of `[suite, subsuite, workload, cluster_id, simulation_type, warmup]` selectors resolved against `workloads/workloads_db.json`. Nulls mean "all".
- `configurations` — named Scarab param sweeps (each has `params`, `binary`, `slurm_options`).
- `visualize` / `collect_stats` / `mem` / `perf_analyze` — settings consumed by the respective `--` commands.

## Architecture / code map

- **`sci`** — CLI: argument parsing, env bootstrap (`--init`), Scarab build coordination, status/visualize/perf-analyze reporting. Calls into `scripts/`.
- **`scripts/`** — the orchestration engine:
  - `utilities.py` — the core (~2700 lines): descriptor parsing, docker image prep, `build_scarab_binary`/`rebuild_scarab`, `prepare_simulation`, simpoint→job expansion, status parsing, memory scheduling, trace prep. Most shared logic lives here.
  - `local_runner.py` / `slurm_runner.py` — the two `workload_manager` backends; each implements `run_simulation`, `print_status`, `kill_jobs`, cleanup. `run_simulation.py` dispatches to one of them.
  - `run_trace.py`, `run_perf.py`, `run_db.py`, `prepare_docker_image.py`, `docker_cleaner.py` — trace pipeline, perf container, stats DB, image build/pull, cleanup.
- **`workloads/`** — one subdirectory per benchmark suite (each with a `Dockerfile` + entrypoint scripts), plus:
  - `workloads_db.json` — master DB: `suite → subsuite → workload → {simulation modes, simpoints[]}`. Every simulation selector resolves here. `workloads_top_simp.json` holds the reduced top-N-simpoint variant (`top_simpoint: true`).
- **`common/`** — shared base Dockerfiles (`Dockerfile.common`, etc.) layered by workload images.
- **`scarab_stats/`** — post-processing: `scarab_stats.py` (`Experiment`, `stat_aggregator` — parses Scarab stat outputs into pandas), `stat_collector.py`, a Jupyter notebook + `serve_jupyter.sh`.
- **`scarab_builds/`** — cache of prebuilt Scarab binaries keyed by git hash + build mode (gitignored). Named `scarab_<hash>_<n>.<mode>` and `pin_exec_*`.
- **`fingerprint_src/`** — small C++ tool (`fpg.cpp`) for trace fingerprinting/clustering.

## Execution model

Everything runs **inside Docker containers**, one image per workload group, tagged `<docker_prefix>:<git_hash_of_scarab-infra>`. `--build-scarab`/`--sim` auto-pull or rebuild the needed image. A simulation fans out into one container/job per (configuration × workload × simpoint); the `manual` runner launches them locally in parallel, the `slurm` runner submits them via `sbatch`. Container names encode `<prefix>_<...>_<job_name>_<...>_<user>`, which is how status/kill/clean commands find them. Build artifacts and binary caching are keyed by the **Scarab** repo's git hash, so changing `scarab_path`'s HEAD triggers a rebuild.

## CRITICAL: stale simulation dirs after source changes

After changing Scarab source code, you **must** delete the existing simulation directory before rerunning a descriptor:

```
rm -rf ~/simulations/<descriptor>      # i.e. <root_dir>/simulations/<experiment>
```

Otherwise `./sci --sim <descriptor>` **silently reuses the old build/results** and you will measure the previous version of the code with no error or warning. Always wipe the experiment's `simulations/` dir when you want a clean rerun of modified source.

## Environment

- Conda env `scarabinfra` (Python 3.12) from `quickstart_env.yaml`; `pip` `requirements.txt` is just `docker`, `psutil`. `./sci --init` sets up everything (Docker, conda env, SSH key, optional Slurm/ghcr.io/trace download).
- SimPoint traces live under `$trace_home` (default `~/traces`), pointed to by `traces_dir`.
- Further docs: `docs/README.trace.md`, `docs/README.perf.md`, `docs/README.ISCA.md`, `docs/slurm_install_guide.md`, and `scarab_stats/README.md`.
