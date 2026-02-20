#!/usr/bin/env python3
"""
run_pipeline.py — Unified DESTINY sweep-to-Pareto pipeline.

Modes
-----
  Full fresh run (default):
      Reads pipeline_config.json, patches + compiles sweep_destiny.cpp,
      runs the sweep, then post-processes results.

  Use existing CSVs (--use-existing-csvs PATH):
      Skips build and sweep; jumps straight to post-processing.

  Skip recompile (--skip-build):
      Assumes sweep_destiny binary already exists; skips compilation.

Usage examples
--------------
  # Complete fresh run using default config
  python run_pipeline.py

  # Fresh run with a custom config file
  python run_pipeline.py --config my_sweep.json

  # Skip recompile (binary already built)
  python run_pipeline.py --skip-build

  # Use previously generated CSVs
  python run_pipeline.py --use-existing-csvs destiny_3d_cache/config/sweep_out/full_csvs/

  # Use existing CSVs, skip Pareto
  python run_pipeline.py --use-existing-csvs ... --no-pareto

  # Use existing CSVs, custom Pareto metrics
  python run_pipeline.py --use-existing-csvs ... --pareto-metrics cacheHitLatency_ns cacheArea_mm2

Config file format (pipeline_config.json)
-----------------------------------------
  {
    "sweep": {
      "cfg_templates": ["sample_2D_eDRAM.cfg", ...],
      "capacities_kb": [32, 64, 128, 256, 512, 1024],
      "capacities_mb": [1, 2, 4, 8],
      "sweep_opt_targets": true,
      "opt_targets": ["WriteEDP", "ReadLatency", ...],
      "swap_cell_files": false,
      "cell_files_to_try": [],
      "max_failures": 10
    },
    "postprocess": {
      "pareto_metrics": ["cacheHitLatency_ns", "cacheLeakage_mW", "cacheArea_mm2"]
    }
  }
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults (mirror the hardcoded values in sweep_destiny.cpp)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = REPO_ROOT / "pipeline_config.json"
DEFAULT_CONFIG_DIR = REPO_ROOT / "destiny_3d_cache" / "config"
CPP_SRC = REPO_ROOT / "sweeps" / "sweep_destiny.cpp"
SWEEP_BINARY_NAME = "sweep_destiny"

DEFAULT_SWEEP_CFG = {
    "cfg_templates": [
        "sample_2D_eDRAM.cfg",
        "sample_3D_eDRAM.cfg",
        "sample_2DReRAM.cfg",
        "sample_3DReRAM.cfg",
        "sample_PCRAM.cfg",
        "sample_STTRAM.cfg",
        "sample_SRAM_2layer.cfg",
        "sample_SRAM_4layer.cfg",
    ],
    "capacities_kb": [32, 64, 128, 256, 512, 1024],
    "capacities_mb": [1, 2, 4, 8],
    "sweep_opt_targets": True,
    "opt_targets": [
        "WriteEDP",
        "ReadLatency",
        "WriteLatency",
        "ReadDynamicEnergy",
        "WriteDynamicEnergy",
        "Full",
    ],
    "swap_cell_files": False,
    "cell_files_to_try": [],
    "max_failures": 10,
}

DEFAULT_POSTPROCESS_CFG = {
    "pareto_metrics": [
        "cacheHitLatency_ns",
        "cacheLeakage_mW",
        "cacheArea_mm2",
    ],
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> tuple[dict, dict]:
    """Load pipeline_config.json and merge with defaults.

    Returns (sweep_cfg, postprocess_cfg).
    """
    raw: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = json.load(f)
    elif config_path != DEFAULT_CONFIG_FILE:
        # User explicitly specified a file that doesn't exist — hard error.
        print(f"[ERROR] Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    sweep_cfg = {**DEFAULT_SWEEP_CFG, **raw.get("sweep", {})}
    post_cfg = {**DEFAULT_POSTPROCESS_CFG, **raw.get("postprocess", {})}
    return sweep_cfg, post_cfg


# ---------------------------------------------------------------------------
# C++ source patching
# ---------------------------------------------------------------------------

def _cpp_string_vec(values: list[str]) -> str:
    """Render a C++ brace-initializer list of string literals."""
    lines = [f'    "{v}",' for v in values]
    return "{\n" + "\n".join(lines) + "\n}"


def _cpp_double_vec(values: list[float | int]) -> str:
    """Render a C++ brace-initializer list of doubles (inline form)."""
    return "{" + ", ".join(str(v) for v in values) + "}"


def _cpp_bool(value: bool) -> str:
    return "true" if value else "false"


def _sub_string_vec(src: str, varname: str, values: list[str]) -> str:
    """Replace a multi-line vector<string> initializer in C++ source."""
    pattern = (
        r"(static const std::vector<std::string>\s+" + re.escape(varname) + r"\s*=\s*)"
        r"\{[^}]*\}"
    )
    replacement = r"\g<1>" + _cpp_string_vec(values)
    result, n = re.subn(pattern, replacement, src, flags=re.DOTALL)
    if n == 0:
        print(f"[WARN] Could not patch {varname} in C++ source — pattern not found.", file=sys.stderr)
    return result


def _sub_double_vec(src: str, varname: str, values: list) -> str:
    """Replace an inline vector<double> initializer in C++ source."""
    pattern = (
        r"(static const std::vector<double>\s+" + re.escape(varname) + r"\s*=\s*)"
        r"\{[^}]*\}"
    )
    replacement = r"\g<1>" + _cpp_double_vec(values)
    result, n = re.subn(pattern, replacement, src)
    if n == 0:
        print(f"[WARN] Could not patch {varname} in C++ source — pattern not found.", file=sys.stderr)
    return result


def _sub_bool(src: str, varname: str, value: bool) -> str:
    pattern = (
        r"(static const bool\s+" + re.escape(varname) + r"\s*=\s*)"
        r"(true|false)"
        r"(\s*;)"
    )
    replacement = r"\g<1>" + _cpp_bool(value) + r"\g<3>"
    result, n = re.subn(pattern, replacement, src)
    if n == 0:
        print(f"[WARN] Could not patch {varname} in C++ source — pattern not found.", file=sys.stderr)
    return result


def _sub_max_failures(src: str, value: int | None) -> str:
    pattern = r"(static const std::optional<int>\s+MAX_FAILURES\s*=\s*)(\d+|std::nullopt)(\s*;)"
    replacement = r"\g<1>" + (str(value) if value is not None else "std::nullopt") + r"\g<3>"
    result, n = re.subn(pattern, replacement, src)
    if n == 0:
        print("[WARN] Could not patch MAX_FAILURES in C++ source — pattern not found.", file=sys.stderr)
    return result


def patch_cpp_source(src: str, sweep_cfg: dict) -> str:
    """Apply all USER SETTINGS substitutions and return the patched source."""
    src = _sub_string_vec(src, "CFG_TEMPLATES", sweep_cfg["cfg_templates"])
    src = _sub_bool(src, "SWEEP_OPT_TARGETS", sweep_cfg["sweep_opt_targets"])
    src = _sub_string_vec(src, "OPT_TARGETS", sweep_cfg["opt_targets"])
    src = _sub_double_vec(src, "CAPACITIES_KB", sweep_cfg["capacities_kb"])
    src = _sub_double_vec(src, "CAPACITIES_MB", sweep_cfg["capacities_mb"])
    src = _sub_bool(src, "SWAP_CELL_FILES", sweep_cfg["swap_cell_files"])
    src = _sub_string_vec(src, "CELL_FILES_TO_TRY", sweep_cfg["cell_files_to_try"])
    src = _sub_max_failures(src, sweep_cfg.get("max_failures", 10))
    return src


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def build_sweeper(config_dir: Path, sweep_cfg: dict) -> None:
    """Patch sweep_destiny.cpp with config values, compile to config_dir/sweep_destiny."""
    print(f"\n  Source : {CPP_SRC}")
    print(f"  Binary : {config_dir / SWEEP_BINARY_NAME}")

    if not CPP_SRC.exists():
        print(f"[ERROR] C++ source not found: {CPP_SRC}", file=sys.stderr)
        sys.exit(1)

    src = CPP_SRC.read_text()
    patched = patch_cpp_source(src, sweep_cfg)

    binary_path = config_dir / SWEEP_BINARY_NAME

    # Write patched source to a temp file, then compile.
    with tempfile.NamedTemporaryFile(
        suffix=".cpp", prefix="sweep_destiny_patched_", delete=False, mode="w"
    ) as tmp:
        tmp.write(patched)
        tmp_path = Path(tmp.name)

    try:
        _compile(tmp_path, binary_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _compile(src_path: Path, binary_path: Path) -> None:
    """Run g++ and raise SystemExit on failure. Retries with -lstdc++fs."""
    base_cmd = ["g++", "-std=gnu++17", "-O2", "-Wall", str(src_path), "-o", str(binary_path)]

    result = subprocess.run(base_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("  Compile: OK")
        return

    # Retry with -lstdc++fs for older GCC toolchains.
    print("  Compile: first attempt failed, retrying with -lstdc++fs …")
    result2 = subprocess.run(base_cmd + ["-lstdc++fs"], capture_output=True, text=True)
    if result2.returncode == 0:
        print("  Compile: OK (with -lstdc++fs)")
        return

    # Both failed — print errors and exit.
    print("[ERROR] Compilation failed.", file=sys.stderr)
    if result2.stderr:
        print(result2.stderr, file=sys.stderr)
    sys.exit(1)


def run_sweep(config_dir: Path) -> None:
    """Run the sweep binary from config_dir (CWD is critical for DESTINY path resolution)."""
    binary = config_dir / SWEEP_BINARY_NAME
    if not binary.exists():
        print(f"[ERROR] Sweeper binary not found: {binary}", file=sys.stderr)
        print("        Run without --skip-build to compile it first.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Binary : {binary}")
    print(f"  CWD    : {config_dir}")
    print()

    proc = subprocess.Popen(
        [f"./{SWEEP_BINARY_NAME}"],
        cwd=config_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()

    if proc.returncode != 0:
        print(f"\n[ERROR] Sweeper exited with code {proc.returncode}.", file=sys.stderr)
        sys.exit(proc.returncode)


def run_postprocess(csv_dir: Path, out_dir: Path, pareto_metrics: list[str]) -> None:
    """Import postprocess_destiny from the repo root and run its pipeline."""
    # Make the repo root importable so `import postprocess_destiny` works.
    repo_root_str = str(REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    try:
        from postprocess_destiny import process_csvs  # type: ignore[import]
    except ImportError as exc:
        print(f"[ERROR] Could not import postprocess_destiny: {exc}", file=sys.stderr)
        print(f"        Expected at: {REPO_ROOT / 'postprocess_destiny.py'}", file=sys.stderr)
        sys.exit(1)

    print(f"\n  CSV dir: {csv_dir}")
    print(f"  Out dir: {out_dir}")
    if pareto_metrics:
        print(f"  Pareto metrics: {', '.join(pareto_metrics)}")
    else:
        print("  Pareto: disabled (--no-pareto)")
    print()

    process_csvs(csv_dir, out_dir, pareto_metrics)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description=(
            "Unified DESTINY pipeline: build sweeper → run sweep → Pareto analysis.\n"
            "Pass --use-existing-csvs to skip the build/sweep and reuse prior results."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--use-existing-csvs",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Path to a directory of previously generated exploration CSVs. "
            "Skips the build and sweep phases entirely."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help=f"Pipeline config JSON file (default: {DEFAULT_CONFIG_FILE.name})",
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
        help="Skip compilation of sweep_destiny.cpp (use the existing binary).",
    )
    parser.add_argument(
        "--no-pareto",
        action="store_true",
        help="Skip Pareto pruning; only combine and clean the CSVs.",
    )
    parser.add_argument(
        "--pareto-metrics",
        nargs="+",
        metavar="METRIC",
        default=None,
        help=(
            "Column names to minimize in the Pareto filter "
            "(overrides postprocess.pareto_metrics from config). "
            "Example: --pareto-metrics cacheHitLatency_ns cacheArea_mm2"
        ),
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Output directory for postprocessed results "
            "(default: <csv_dir>/../postprocessed/)"
        ),
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  DESTINY Pipeline")
    print("=" * 60)

    # ---- Load config -------------------------------------------------------
    sweep_cfg, post_cfg = load_config(args.config)

    # Resolve Pareto metrics: CLI > config file > built-in default.
    if args.no_pareto:
        pareto_metrics: list[str] = []
    elif args.pareto_metrics is not None:
        pareto_metrics = args.pareto_metrics
    else:
        pareto_metrics = post_cfg["pareto_metrics"]

    config_dir: Path = args.config_dir.resolve()

    # ---- Determine mode and step count ------------------------------------
    if args.use_existing_csvs is not None:
        csv_dir = args.use_existing_csvs.resolve()
        mode = "existing"
        total_steps = 1
    else:
        csv_dir = config_dir / "sweep_out" / "full_csvs"
        mode = "fresh"
        if args.skip_build:
            total_steps = 2
        else:
            total_steps = 3

    out_dir = (args.out or csv_dir.parent / "postprocessed").resolve()

    step = 0

    # ---- Build -------------------------------------------------------------
    if mode == "fresh" and not args.skip_build:
        step += 1
        print(f"\n[STEP {step}/{total_steps}] Build sweeper")
        print(f"  Config : {args.config}")
        build_sweeper(config_dir, sweep_cfg)

    # ---- Sweep -------------------------------------------------------------
    if mode == "fresh":
        step += 1
        print(f"\n[STEP {step}/{total_steps}] Run sweep")
        run_sweep(config_dir)

    # ---- Postprocess -------------------------------------------------------
    step += 1
    label = "Postprocess (combine + Pareto)" if pareto_metrics else "Postprocess (combine only)"
    print(f"\n[STEP {step}/{total_steps}] {label}")
    run_postprocess(csv_dir, out_dir, pareto_metrics)

    # ---- Summary -----------------------------------------------------------
    print("\n" + "=" * 60)
    print("  Pipeline complete.")
    print("=" * 60)
    combined_dir = out_dir / "combined"
    pareto_dir = out_dir / "pareto"
    print(f"\n  Combined CSVs : {combined_dir}")
    if pareto_metrics:
        print(f"  Pareto CSVs   : {pareto_dir}")
    if mode == "fresh":
        sweep_csv = config_dir / "sweep_out" / "destiny_sweep_results.csv"
        print(f"  Sweep summary : {sweep_csv}")
    print()


if __name__ == "__main__":
    main()
