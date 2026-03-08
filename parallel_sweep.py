#!/usr/bin/env python3
"""
parallel_sweep.py — Parallel DESTINY sweep by memory technology.

Partitions work by cfg template (one process per memory technology) and
runs all workers concurrently via concurrent.futures.ProcessPoolExecutor.
Each worker calls the compiled sweep_destiny binary with --templates and
--capacities-* flags so it only processes its assigned technology.

Usage
-----
  python parallel_sweep.py                      # all templates, max parallelism
  python parallel_sweep.py --workers 4          # cap at 4 parallel processes
  python parallel_sweep.py --config foo.json    # custom config file
  python parallel_sweep.py --skip-build         # binary already compiled
  python parallel_sweep.py --no-pareto          # skip Pareto pruning
  python parallel_sweep.py --config-dir PATH    # custom DESTINY config dir

Config file format
------------------
  Same as pipeline_config.json used by run_pipeline.py.
"""

import argparse
import concurrent.futures
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = REPO_ROOT / "pipeline_config.json"
DEFAULT_CONFIG_DIR = REPO_ROOT / "destiny_3d_cache" / "config"
CPP_SRC = REPO_ROOT / "sweeps" / "sweep_destiny.cpp"
SWEEP_BINARY_NAME = "sweep_destiny"


# ---------------------------------------------------------------------------
# Worker — must be module-level for ProcessPoolExecutor pickling
# ---------------------------------------------------------------------------

def _run_worker(
    binary: str,
    config_dir: str,
    template: str,
    caps_kb: list,
    caps_mb: list,
    worker_idx: int,
) -> tuple:
    """Run sweep_destiny for one template in an isolated output directory.

    Returns (worker_out_dir, returncode, output_text).
    """
    out_dir_name = f"worker_{worker_idx}"

    cmd = [binary, config_dir, "--templates", template]
    if caps_kb:
        cmd += ["--capacities-kb", ",".join(str(c) for c in caps_kb)]
    if caps_mb:
        cmd += ["--capacities-mb", ",".join(str(c) for c in caps_mb)]
    cmd += ["--out-dir", out_dir_name]

    proc = subprocess.run(cmd, cwd=config_dir, capture_output=True, text=True)
    output = proc.stdout
    if proc.stderr.strip():
        output += "\n[stderr]\n" + proc.stderr

    return (str(Path(config_dir) / out_dir_name), proc.returncode, output)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _build(config_dir: Path, sweep_cfg: dict) -> None:
    """Patch sweep_destiny.cpp for shared settings and compile once."""
    from run_pipeline import patch_cpp_source, _compile  # type: ignore[import]

    print(f"  Source : {CPP_SRC}")
    print(f"  Binary : {config_dir / SWEEP_BINARY_NAME}")

    if not CPP_SRC.exists():
        print(f"[ERROR] C++ source not found: {CPP_SRC}", file=sys.stderr)
        sys.exit(1)

    src = CPP_SRC.read_text()
    patched = patch_cpp_source(src, sweep_cfg)

    with tempfile.NamedTemporaryFile(
        suffix=".cpp", prefix="sweep_destiny_patched_", delete=False, mode="w"
    ) as tmp:
        tmp.write(patched)
        tmp_path = Path(tmp.name)

    try:
        _compile(tmp_path, config_dir / SWEEP_BINARY_NAME)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Merge worker outputs
# ---------------------------------------------------------------------------

def _merge_worker_outputs(worker_dirs: list, config_dir: Path) -> Path:
    """Copy per-run exploration CSVs from each worker into a shared directory.

    Because each worker handles a distinct template, CSV filenames are unique
    across workers (they embed the template stem), so no deduplication needed.

    Returns the merged full_csvs directory path.
    """
    merged = config_dir / "sweep_out" / "full_csvs"
    merged.mkdir(parents=True, exist_ok=True)

    total = 0
    for wdir_str in worker_dirs:
        src_dir = Path(wdir_str) / "full_csvs"
        if not src_dir.is_dir():
            continue
        for csv_file in src_dir.glob("*.csv"):
            shutil.copy2(csv_file, merged / csv_file.name)
            total += 1

    print(f"  Merged {total} exploration CSV(s) → {merged}")
    return merged


# ---------------------------------------------------------------------------
# Postprocess
# ---------------------------------------------------------------------------

def _postprocess(csv_dir: Path, out_dir: Path, pareto_metrics: list) -> None:
    try:
        from postprocess_destiny import process_csvs  # type: ignore[import]
    except ImportError as exc:
        print(f"[ERROR] Could not import postprocess_destiny: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  CSV dir : {csv_dir}")
    print(f"  Out dir : {out_dir}")
    if pareto_metrics:
        print(f"  Pareto  : {', '.join(pareto_metrics)}")
    else:
        print("  Pareto  : disabled")
    print()

    process_csvs(csv_dir, out_dir, pareto_metrics)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="parallel_sweep.py",
        description=(
            "Parallel DESTINY pipeline: compile once, sweep all memory technologies "
            "concurrently (one process per template), then postprocess results."
        ),
    )
    parser.add_argument(
        "--config", metavar="FILE", type=Path, default=DEFAULT_CONFIG_FILE,
        help=f"Pipeline config JSON (default: {DEFAULT_CONFIG_FILE.name})",
    )
    parser.add_argument(
        "--config-dir", metavar="PATH", type=Path, default=DEFAULT_CONFIG_DIR,
        help="DESTINY config directory (default: destiny_3d_cache/config/)",
    )
    parser.add_argument(
        "--workers", metavar="N", type=int, default=None,
        help="Max parallel processes (default: one per template)",
    )
    parser.add_argument(
        "--skip-build", action="store_true",
        help="Skip compilation; use the existing sweep_destiny binary.",
    )
    parser.add_argument(
        "--no-pareto", action="store_true",
        help="Skip Pareto pruning; only combine and clean the CSVs.",
    )
    parser.add_argument(
        "--pareto-metrics", nargs="+", metavar="METRIC", default=None,
        help="Columns to minimize in Pareto filter (overrides config).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # Add repo root to path so we can import run_pipeline / postprocess_destiny.
    repo_root_str = str(REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    from run_pipeline import load_config  # type: ignore[import]

    print("=" * 60)
    print("  DESTINY Parallel Sweep")
    print("=" * 60)

    sweep_cfg, post_cfg = load_config(args.config)
    config_dir = args.config_dir.resolve()
    binary = str(config_dir / SWEEP_BINARY_NAME)

    templates = sweep_cfg["cfg_templates"]
    caps_kb: list = sweep_cfg.get("capacities_kb", [])
    caps_mb: list = sweep_cfg.get("capacities_mb", [])
    max_workers = args.workers or len(templates)

    if args.no_pareto:
        pareto_metrics: list = []
    elif args.pareto_metrics is not None:
        pareto_metrics = args.pareto_metrics
    else:
        pareto_metrics = post_cfg["pareto_metrics"]

    skip_build = args.skip_build
    total_steps = 2 if skip_build else 3
    step = 0

    # -- Build ---------------------------------------------------------------
    if not skip_build:
        step += 1
        print(f"\n[STEP {step}/{total_steps}] Build sweeper")
        print(f"  Config : {args.config}")
        _build(config_dir, sweep_cfg)

    # -- Parallel sweep ------------------------------------------------------
    step += 1
    print(f"\n[STEP {step}/{total_steps}] Parallel sweep")
    print(f"  Templates : {len(templates)}")
    print(f"  Workers   : {max_workers}")
    print(f"  Config dir: {config_dir}")
    print()

    t0 = time.time()
    succeeded: list[str] = []
    failed: list[str] = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
        future_to_tmpl = {
            pool.submit(
                _run_worker,
                binary, str(config_dir),
                tmpl, caps_kb, caps_mb, i,
            ): tmpl
            for i, tmpl in enumerate(templates)
        }

        for future in concurrent.futures.as_completed(future_to_tmpl):
            tmpl = future_to_tmpl[future]
            try:
                worker_out, rc, output = future.result()
            except Exception as exc:
                print(f"  [FAIL] {tmpl}: {exc}")
                failed.append(tmpl)
                continue

            if rc == 0:
                print(f"  [ok]   {tmpl}")
                succeeded.append(worker_out)
            else:
                print(f"  [FAIL] {tmpl} (exit {rc})")
                tail = "\n         ".join(output.splitlines()[-10:])
                if tail:
                    print(f"         {tail}")
                failed.append(tmpl)

    elapsed = time.time() - t0
    est_sequential = elapsed * len(templates) / max(len(succeeded), 1) if succeeded else elapsed
    print(
        f"\n  {len(succeeded)}/{len(templates)} succeeded in {elapsed:.1f}s"
        f"  (est. sequential: {est_sequential:.0f}s,"
        f" speedup: {est_sequential / elapsed:.1f}x)"
    )

    if not succeeded:
        print("[ERROR] All workers failed. Aborting.", file=sys.stderr)
        sys.exit(1)

    if failed:
        print(f"[WARN] {len(failed)} template(s) failed: {', '.join(failed)}", file=sys.stderr)

    # -- Merge + postprocess -------------------------------------------------
    step += 1
    print(f"\n[STEP {step}/{total_steps}] Merge CSVs and postprocess")
    merged_dir = _merge_worker_outputs(succeeded, config_dir)
    out_dir = (config_dir / "sweep_out" / "postprocessed").resolve()
    _postprocess(merged_dir, out_dir, pareto_metrics)


if __name__ == "__main__":
    main()
