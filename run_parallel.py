#!/usr/bin/env python3
"""
run_parallel.py — Parallelized DESTINY sweep pipeline.

Partitions the configured templates (one per memory technology) across N
workers, runs the sweep binary in parallel for each partition, merges the
results, then post-processes identical to run_pipeline.py.

Usage
-----
  # Default: one worker per memory technology template (up to 8 in parallel)
  python run_parallel.py

  # Limit to 4 parallel workers
  python run_parallel.py --workers 4

  # Skip recompile (binary already built)
  python run_parallel.py --skip-build

  # Use previously generated CSVs
  python run_parallel.py --use-existing-csvs destiny_3d_cache/config/sweep_out/full_csvs/

  # Custom config file
  python run_parallel.py --config my_sweep.json

How parallelism works
---------------------
The template list from pipeline_config.json is split round-robin across N
workers.  Each worker calls the sweep_destiny binary with:

    ./sweep_destiny --templates <t1,t2,...> --out-dir sweep_out_worker_<id>

so parallel runs never write to the same directory.  After all workers
finish, their destiny_sweep_results.csv, full_csvs/, and logs/ are merged
into a single sweep_out/ directory, which is then passed to the existing
postprocess_destiny pipeline.
"""

import argparse
import csv
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Re-use helpers from run_pipeline.py
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from run_pipeline import (  # noqa: E402
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILE,
    SWEEP_BINARY_NAME,
    build_sweeper,
    load_config,
    run_postprocess,
)

MERGED_OUT_DIR_NAME = "sweep_out"


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------

def partition_templates(templates: list[str], n_workers: int) -> list[list[str]]:
    """Split templates into up to n_workers non-empty groups (round-robin)."""
    n = min(n_workers, len(templates))
    partitions: list[list[str]] = [[] for _ in range(n)]
    for i, t in enumerate(templates):
        partitions[i % n].append(t)
    return partitions


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def run_worker(
    binary: Path,
    config_dir: Path,
    templates: list[str],
    worker_id: int,
) -> tuple[int, str]:
    """Run sweep_destiny for a subset of templates, writing to its own output dir.

    Returns (return_code, out_dir_name).
    """
    out_dir_name = f"sweep_out_worker_{worker_id}"
    templates_arg = ",".join(templates)
    prefix = f"[worker-{worker_id}]"

    print(f"{prefix} Starting: {', '.join(templates)}", flush=True)

    proc = subprocess.Popen(
        [f"./{SWEEP_BINARY_NAME}", "--templates", templates_arg, "--out-dir", out_dir_name],
        cwd=config_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"{prefix} {line}", end="", flush=True)
    proc.wait()

    if proc.returncode != 0:
        print(f"{prefix} [ERROR] exited with code {proc.returncode}", flush=True)
    else:
        print(f"{prefix} Done.", flush=True)

    return proc.returncode, out_dir_name


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_worker_outputs(
    config_dir: Path,
    worker_out_dirs: list[str],
    merged_dir: Path,
) -> None:
    """Merge per-worker sweep_out directories into a single directory."""
    merged_dir.mkdir(parents=True, exist_ok=True)
    (merged_dir / "full_csvs").mkdir(exist_ok=True)
    (merged_dir / "logs").mkdir(exist_ok=True)

    all_rows: list[list[str]] = []
    header: list[str] | None = None

    for out_dir_name in worker_out_dirs:
        worker_dir = config_dir / out_dir_name

        # Merge destiny_sweep_results.csv
        results_csv = worker_dir / "destiny_sweep_results.csv"
        if results_csv.exists():
            with open(results_csv, newline="") as f:
                rows = list(csv.reader(f))
            if rows:
                if header is None:
                    header = rows[0]
                all_rows.extend(rows[1:])

        # Copy full_csvs (DESTINY exploration CSVs)
        src_full = worker_dir / "full_csvs"
        if src_full.exists():
            for f in src_full.iterdir():
                shutil.copy2(f, merged_dir / "full_csvs" / f.name)

        # Copy logs
        src_logs = worker_dir / "logs"
        if src_logs.exists():
            for f in src_logs.iterdir():
                shutil.copy2(f, merged_dir / "logs" / f.name)

    if header is not None:
        merged_csv = merged_dir / "destiny_sweep_results.csv"
        with open(merged_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(all_rows)
        print(f"\n  Merged {len(all_rows)} rows → {merged_csv}")
    else:
        print("\n  [WARN] No rows found in any worker output.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_parallel.py",
        description=(
            "Parallelized DESTINY pipeline: builds once, sweeps in parallel by "
            "memory technology, merges results, then runs Pareto analysis."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Number of parallel workers (default: one per template).",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help=f"Pipeline config JSON (default: {DEFAULT_CONFIG_FILE.name})",
    )
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=(
            "Path to the DESTINY config directory where the sweeper binary "
            f"is placed and run from (default: {DEFAULT_CONFIG_DIR.relative_to(REPO_ROOT)})"
        ),
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip recompilation (use existing binary).",
    )
    parser.add_argument(
        "--use-existing-csvs",
        metavar="PATH",
        type=Path,
        default=None,
        help="Skip build/sweep and jump straight to postprocessing.",
    )
    parser.add_argument(
        "--no-pareto",
        action="store_true",
        help="Skip Pareto pruning.",
    )
    parser.add_argument(
        "--pareto-metrics",
        nargs="+",
        metavar="METRIC",
        default=None,
        help=(
            "Columns to minimize in Pareto filter (overrides config). "
            "Example: --pareto-metrics cacheHitLatency_ns cacheArea_mm2"
        ),
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        type=Path,
        default=None,
        help="Output directory for postprocessed results.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  DESTINY Parallel Pipeline")
    print("=" * 60)

    sweep_cfg, post_cfg = load_config(args.config)

    if args.no_pareto:
        pareto_metrics: list[str] = []
    elif args.pareto_metrics is not None:
        pareto_metrics = args.pareto_metrics
    else:
        pareto_metrics = post_cfg["pareto_metrics"]

    config_dir: Path = args.config_dir.resolve()

    # ---- Use existing CSVs (skip build + sweep) ----------------------------
    if args.use_existing_csvs is not None:
        csv_dir = args.use_existing_csvs.resolve()
        out_dir = (args.out or csv_dir.parent / "postprocessed").resolve()
        print("\n[STEP 1/1] Postprocess (using existing CSVs)")
        run_postprocess(csv_dir, out_dir, pareto_metrics)
        _print_summary(out_dir, pareto_metrics, csv_dir=csv_dir)
        return

    templates = sweep_cfg["cfg_templates"]
    n_workers = args.workers or len(templates)
    partitions = partition_templates(templates, n_workers)

    total_steps = 2 if args.skip_build else 3
    step = 0

    # ---- Build (once) ------------------------------------------------------
    if not args.skip_build:
        step += 1
        print(f"\n[STEP {step}/{total_steps}] Build sweeper")
        print(f"  Config : {args.config}")
        build_sweeper(config_dir, sweep_cfg)

    # ---- Parallel sweep ----------------------------------------------------
    step += 1
    print(f"\n[STEP {step}/{total_steps}] Parallel sweep ({len(partitions)} workers)")
    print(f"  Workers  : {len(partitions)}")
    print(f"  Templates: {len(templates)}")
    for i, part in enumerate(partitions):
        print(f"    worker-{i}: {', '.join(part)}")
    print()

    binary = config_dir / SWEEP_BINARY_NAME
    if not binary.exists():
        print(f"[ERROR] Binary not found: {binary}", file=sys.stderr)
        print("        Run without --skip-build to compile it first.", file=sys.stderr)
        sys.exit(1)

    worker_out_dirs: list[str] = []
    failed_workers: list[int] = []

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(partitions)) as pool:
        future_to_id = {
            pool.submit(run_worker, binary, config_dir, part, i): i
            for i, part in enumerate(partitions)
        }
        for future in as_completed(future_to_id):
            worker_id = future_to_id[future]
            rc, out_dir_name = future.result()
            worker_out_dirs.append(out_dir_name)
            if rc != 0:
                failed_workers.append(worker_id)

    elapsed = time.time() - t0
    print(f"\n  All workers finished in {elapsed:.1f}s")
    if failed_workers:
        print(f"  [WARN] Workers with non-zero exit: {failed_workers}", file=sys.stderr)

    # ---- Merge worker outputs ----------------------------------------------
    merged_dir = config_dir / MERGED_OUT_DIR_NAME
    print(f"\n  Merging {len(worker_out_dirs)} worker output(s) → {merged_dir}")
    merge_worker_outputs(config_dir, worker_out_dirs, merged_dir)

    # ---- Postprocess -------------------------------------------------------
    step += 1
    csv_dir = merged_dir / "full_csvs"
    out_dir = (args.out or merged_dir / "postprocessed").resolve()
    label = "Postprocess (combine + Pareto)" if pareto_metrics else "Postprocess (combine only)"
    print(f"\n[STEP {step}/{total_steps}] {label}")
    run_postprocess(csv_dir, out_dir, pareto_metrics)

    _print_summary(out_dir, pareto_metrics, sweep_dir=merged_dir)


def _print_summary(
    out_dir: Path,
    pareto_metrics: list[str],
    csv_dir: Path | None = None,
    sweep_dir: Path | None = None,
) -> None:
    print("\n" + "=" * 60)
    print("  Pipeline complete.")
    print("=" * 60)
    print(f"\n  Combined CSVs : {out_dir / 'combined'}")
    if pareto_metrics:
        print(f"  Pareto CSVs   : {out_dir / 'pareto'}")
    if sweep_dir:
        print(f"  Sweep summary : {sweep_dir / 'destiny_sweep_results.csv'}")
    print()


if __name__ == "__main__":
    main()
