#!/usr/bin/env python3
"""
postprocess_destiny.py

Unified post-processing for DESTINY exploration CSVs produced by sweep_destiny.

Pipeline:
  1. Load raw CSVs from full_csvs/ (no headers, embedded unit strings)
  2. Add correct column headers (cache-mode 94 cols or memory-mode 40 cols)
  3. Strip unit suffixes from numeric values, converting to canonical units
  4. Extract metadata from filenames (capacity, cell_type, opt_target, template)
  5. Combine into per-capacity CSVs
  6. Pareto-prune each capacity group

Usage:
    python postprocess_destiny.py <full_csvs_dir> [--out <output_dir>]
    python postprocess_destiny.py sweep_out/full_csvs/
    python postprocess_destiny.py sweep_out/full_csvs/ --no-pareto
    python postprocess_destiny.py sweep_out/full_csvs/ --pareto-metrics cacheHitLatency_ns cacheLeakage_mW cacheArea_mm2

Replaces: add_destiny_csv_headers.sh + pareto_destiny.py
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
from paretoset import paretoset

# ---------------------------------------------------------------------------
# Column header definitions (ported from add_destiny_csv_headers.sh)
# ---------------------------------------------------------------------------

# Bank-level columns (40), written by DESTINY's Result::printToCsvFile
BANK_COLUMNS = [
    "numRowMat", "numColumnMat", "stackedDieCount",
    "numActiveMatPerColumn", "numActiveMatPerRow",
    "numRowSubarray", "numColumnSubarray",
    "numActiveSubarrayPerColumn", "numActiveSubarrayPerRow",
    "numRow_subarray", "numColumn_subarray",
    "muxSenseAmp", "muxOutputLev1", "muxOutputLev2", "numRowPerSet",
    "localWireType", "localWireRepeaterType", "localWireLowSwing",
    "globalWireType", "globalWireRepeaterType", "globalWireLowSwing",
    "bufferDesignStyle",
    "bankHeight_um", "bankWidth_um", "bankArea_mm2",
    "matHeight_um", "matWidth_um", "matArea_mm2",
    "subarrayHeight_um", "subarrayWidth_um", "subarrayArea_mm2",
    "areaEfficiency_pct",
    "readLatency_ns", "writeLatency_ns", "refreshLatency_ns",
    "readDynamicEnergy_pJ", "writeDynamicEnergy_pJ", "refreshDynamicEnergy_pJ",
    "leakage_mW", "refreshPower_W",
]

# Cache-level prefix columns (12)
CACHE_PREFIX_COLUMNS = [
    "cacheAccessMode", "cacheArea_mm2",
    "cacheHitLatency_ns", "cacheMissLatency_ns",
    "cacheWriteLatency_ns", "cacheRefreshLatency_ns",
    "cacheHitDynamicEnergy_nJ", "cacheMissDynamicEnergy_nJ",
    "cacheWriteDynamicEnergy_nJ", "cacheRefreshDynamicEnergy_nJ",
    "cacheLeakage_mW", "cacheRefreshPower_W",
]

# Cache-level suffix columns (2)
CACHE_SUFFIX_COLUMNS = [
    "combinedSubarrayLeakage", "combinedSubarrayArea_mm2",
]


def make_cache_header() -> list[str]:
    """Cache-mode header: 12 cache + 40 data_bank + 40 tag_bank + 2 combined = 94 cols."""
    return (
        CACHE_PREFIX_COLUMNS
        + [f"data_{c}" for c in BANK_COLUMNS]
        + [f"tag_{c}" for c in BANK_COLUMNS]
        + CACHE_SUFFIX_COLUMNS
    )


def make_memory_header() -> list[str]:
    """Memory (non-cache) mode header: 40 cols."""
    return list(BANK_COLUMNS)


# ---------------------------------------------------------------------------
# Unit conversion (reference: sweep_destiny.cpp lines 175-214)
# ---------------------------------------------------------------------------

# Maps column-name suffix -> {value_suffix: scale_factor_to_target_unit}
UNIT_CONVERSIONS: dict[str, dict[str, float]] = {
    "_ns":  {"ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9},
    "_pJ":  {"pJ": 1.0, "nJ": 1e3, "uJ": 1e6, "mJ": 1e9, "J": 1e12},
    "_nJ":  {"pJ": 1e-3, "nJ": 1.0, "uJ": 1e3, "mJ": 1e6, "J": 1e9},
    "_mW":  {"pW": 1e-9, "nW": 1e-6, "uW": 1e-3, "mW": 1.0, "W": 1e3},
    "_W":   {"pW": 1e-12, "nW": 1e-9, "uW": 1e-6, "mW": 1e-3, "W": 1.0},
    "_um":  {"nm": 1e-3, "um": 1.0, "mm": 1e3},
    "_um2": {"um2": 1.0, "um^2": 1.0, "mm2": 1e6, "mm^2": 1e6},
    "_mm2": {"um2": 1e-6, "um^2": 1e-6, "mm2": 1.0, "mm^2": 1.0},
    "_pct": {"%": 1.0},
}

# Regex to split a value like "123.45pW" into number + unit
_VALUE_UNIT_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(.+)$"
)


def _get_column_unit_suffix(col_name: str) -> str | None:
    """Return the unit suffix if the column name ends with a known unit."""
    for suffix in UNIT_CONVERSIONS:
        if col_name.endswith(suffix):
            return suffix
    return None


def _strip_unit_and_convert(value: str, column_name: str) -> float | str:
    """Parse a value like '123.45pW', strip the unit, convert to the target
    unit indicated by column_name (e.g., column ending in '_mW' → milliwatts).

    Returns float if successful, original string otherwise.
    """
    suffix = _get_column_unit_suffix(column_name)
    if suffix is None:
        return value  # non-numeric column

    value = str(value).strip()
    if not value:
        return value

    conversions = UNIT_CONVERSIONS[suffix]

    m = _VALUE_UNIT_RE.match(value)
    if m:
        num = float(m.group(1))
        unit_str = m.group(2).strip()
        if unit_str in conversions:
            return num * conversions[unit_str]

    # Fallback: try parsing as plain number (already in target unit)
    try:
        return float(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# CSV mode detection (ported from add_destiny_csv_headers.sh)
# ---------------------------------------------------------------------------

_CACHE_ACCESS_MODES = {"Normal", "Fast", "Sequential"}
_KNOWN_HEADER_STARTS = {"cacheAccessMode", "numRowMat"}


def detect_csv_mode(csv_path: Path) -> str:
    """Return 'cache' or 'memory' based on the first field of the first row."""
    with open(csv_path) as f:
        first_line = f.readline().strip()

    if not first_line:
        raise ValueError(f"Empty CSV: {csv_path}")

    first_field = first_line.split(",")[0].strip()

    # Already has a header?
    if first_field in _KNOWN_HEADER_STARTS:
        return "cache" if first_field == "cacheAccessMode" else "memory"

    # Raw CSV: detect from first data field
    if first_field in _CACHE_ACCESS_MODES:
        return "cache"

    return "memory"


# ---------------------------------------------------------------------------
# Filename metadata extraction
# ---------------------------------------------------------------------------

# sweep_destiny.cpp lines 700-709:
#   tmp__<template_stem>__<cell_tag>__<cap><unit>[__<opt_target>].csv
# Cell tag = .cell filename without extension (lines 695-698).
# Double-underscore (__) is the delimiter; names use single underscores only.
_FILENAME_RE = re.compile(
    r"^tmp__"
    r"(?P<template>.+?)"
    r"__(?P<cell_tag>.+?)"
    r"__(?P<capacity>\d+(?:\.\d+)?[KMG]B)"
    r"(?:__(?P<opt_target>\w+))?"
    r"\.csv$"
)


def parse_filename(filename: str) -> dict[str, str | None]:
    """Extract metadata from a DESTINY exploration CSV filename.

    Returns dict with keys: template, cell_type, capacity, opt_target.
    """
    m = _FILENAME_RE.match(filename)
    if not m:
        raise ValueError(f"Cannot parse filename: {filename}")
    return {
        "template": m.group("template"),
        "cell_type": m.group("cell_tag"),
        "capacity": m.group("capacity"),
        "opt_target": m.group("opt_target"),
    }


def capacity_sort_key(cap: str) -> float:
    """Convert capacity string like '128KB' to numeric KB for sorting."""
    m = re.match(r"(\d+(?:\.\d+)?)(KB|MB|GB)", cap)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "MB":
        val *= 1024
    elif unit == "GB":
        val *= 1024 * 1024
    return val


# ---------------------------------------------------------------------------
# Core pipeline functions
# ---------------------------------------------------------------------------

def load_raw_csv(csv_path: Path, headers: list[str]) -> pd.DataFrame:
    """Load a single raw DESTINY CSV, applying headers if missing."""
    with open(csv_path) as f:
        first_field = f.readline().strip().split(",")[0].strip()

    has_header = first_field in _KNOWN_HEADER_STARTS

    if has_header:
        df = pd.read_csv(csv_path)
    else:
        df = pd.read_csv(csv_path, header=None)
        # DESTINY produces a trailing comma → pandas reads an extra column
        if len(df.columns) == len(headers) + 1:
            df = df.iloc[:, :-1]

        if len(df.columns) != len(headers):
            raise ValueError(
                f"{csv_path.name}: expected {len(headers)} columns, "
                f"got {len(df.columns)}"
            )
        df.columns = headers

    # Drop any remaining unnamed trailing columns
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed")]
    if unnamed:
        df = df.drop(columns=unnamed)

    return df


def clean_numeric_values(df: pd.DataFrame) -> pd.DataFrame:
    """Strip unit suffixes from numeric columns, converting to canonical units."""
    _extract_re = r'^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(.+)$'
    for col in df.columns:
        suffix = _get_column_unit_suffix(col)
        if suffix is None:
            continue
        conversions = UNIT_CONVERSIONS[suffix]
        s = df[col].astype(str).str.strip()
        extracted = s.str.extract(_extract_re, expand=True)
        nums = pd.to_numeric(extracted[0], errors='coerce')
        scales = extracted[1].map(conversions)
        converted = nums * scales
        fallback = pd.to_numeric(s, errors='coerce')
        df[col] = converted.where(converted.notna(), fallback)
    return df


# Columns in memory-mode output that should be renamed (and optionally rescaled)
# to match the cache-mode naming convention used downstream.
# Format: old_name -> (new_name, scale_factor)
_MEMORY_COLUMN_REMAP: dict[str, tuple[str, float]] = {
    "readLatency_ns":        ("cacheHitLatency_ns",          1.0),
    "writeLatency_ns":       ("cacheWriteLatency_ns",         1.0),
    "bankArea_mm2":          ("cacheArea_mm2",                1.0),
    "readDynamicEnergy_pJ":  ("cacheHitDynamicEnergy_nJ",    1e-3),
    "writeDynamicEnergy_pJ": ("cacheWriteDynamicEnergy_nJ",  1e-3),
    "leakage_mW":            ("cacheLeakage_mW",              1.0),
}


def remap_memory_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename and rescale memory-mode columns to match cache-mode naming.

    Remapped columns are moved to the front of the DataFrame.
    """
    rename_map = {}
    for old, (new, scale) in _MEMORY_COLUMN_REMAP.items():
        if old not in df.columns:
            continue
        if scale != 1.0:
            df[old] = df[old] * scale
        rename_map[old] = new
    df = df.rename(columns=rename_map)
    new_names = [new for _, (new, _) in _MEMORY_COLUMN_REMAP.items() if new in df.columns]
    remaining = [c for c in df.columns if c not in new_names]
    return df[new_names + remaining]


def add_metadata_columns(df: pd.DataFrame, filename: str) -> pd.DataFrame:
    """Add metadata columns extracted from the filename."""
    meta = parse_filename(filename)
    df["_source_file"] = filename
    df["_capacity"] = meta["capacity"]
    df["_cell_type"] = meta["cell_type"]
    df["_opt_target"] = meta["opt_target"] or ""
    df["_template"] = meta["template"]
    return df


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_csvs(
    csv_dir: Path,
    out_dir: Path,
    pareto_metrics: list[str],
    pareto_senses: list[str] | None = None,
) -> None:
    """Full post-processing pipeline."""
    if pareto_senses is None:
        pareto_senses = ["min"] * len(pareto_metrics)

    combined_dir = out_dir / "combined"
    pareto_dir = out_dir / "pareto"
    combined_dir.mkdir(parents=True, exist_ok=True)
    if pareto_metrics:
        pareto_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: discover all CSVs
    csv_files = sorted(csv_dir.glob("*__Full.csv"))[:20]
    print(f"[INFO] Testing subset: {len(csv_files)} Full CSVs")

    if not csv_files:
        print("[ERROR] No CSV files found.", file=sys.stderr)
        sys.exit(1)

    # Stage 2-4: load, add headers, strip units, add metadata
    frames: list[pd.DataFrame] = []
    skipped = 0
    for csv_path in csv_files:
        try:
            mode = detect_csv_mode(csv_path)
            headers = make_cache_header() if mode == "cache" else make_memory_header()

            df = load_raw_csv(csv_path, headers)
            df = clean_numeric_values(df)
            if mode == "memory":
                df = remap_memory_columns(df)
            df = add_metadata_columns(df, csv_path.name)
            frames.append(df)
        except (ValueError, KeyError) as e:
            print(f"[WARN] Skipping {csv_path.name}: {e}", file=sys.stderr)
            skipped += 1

    if not frames:
        print("[ERROR] No valid CSV files loaded.", file=sys.stderr)
        sys.exit(1)

    all_data = pd.concat(frames, ignore_index=True)
    print(f"Loaded {len(all_data)} rows from {len(frames)} files ({skipped} skipped)")

    # Stage 5-6: group by capacity, write combined CSVs
    capacities = sorted(all_data["_capacity"].unique(), key=capacity_sort_key)

    for cap in capacities:
        group = all_data[all_data["_capacity"] == cap]
        combined_path = combined_dir / f"combined_{cap}.csv"
        group.to_csv(combined_path, index=False)
        print(f"  {cap:>8s}: {len(group):6d} rows -> {combined_path.name}")

    all_combined_path = combined_dir / "combined_all.csv"
    all_data.to_csv(all_combined_path, index=False)
    print(f"\nWrote combined CSVs to {combined_dir}/")

    # Stage 7-8: Pareto pruning per capacity
    if not pareto_metrics:
        print("\nPareto pruning skipped (--no-pareto).")
        return

    # missing = [m for m in pareto_metrics if m not in all_data.columns]
    # if missing:
    #     print(
    #         f"[WARN] Cannot run Pareto pruning. Missing columns: {missing}",
    #         file=sys.stderr,
    #     )
    #     print(f"  Available columns: {list(all_data.columns)}", file=sys.stderr)
    #     return

    available_metrics = [m for m in pareto_metrics if m in all_data.columns]
    missing_metrics = [m for m in pareto_metrics if m not in all_data.columns]

    if missing_metrics:
        print(f"[WARN] Some Pareto metrics missing globally, will ignore: {missing_metrics}", file=sys.stderr)

    if len(available_metrics) == 0:
        print("[WARN] No Pareto metrics available. Skipping Pareto pruning.", file=sys.stderr)
        return


    # Coerce metric columns to numeric
    for m in available_metrics:
        all_data[m] = pd.to_numeric(all_data[m], errors="coerce")

    all_pareto: list[pd.DataFrame] = []
    for cap in capacities:
        group = all_data[all_data["_capacity"] == cap].copy()
        valid = group.dropna(subset=available_metrics)

        if valid.empty:
            print(f"  {cap:>8s}: no valid rows for Pareto pruning")
            continue

        costs = valid[available_metrics].values
        mask = paretoset(costs, sense=pareto_senses)
        pareto = valid[mask].copy()

        pareto_path = pareto_dir / f"pareto_{cap}.csv"
        pareto.to_csv(pareto_path, index=False)
        all_pareto.append(pareto)

        pct = len(pareto) / len(valid) * 100
        print(
            f"  {cap:>8s}: {len(valid):6d} candidates -> "
            f"{len(pareto):5d} Pareto-optimal ({pct:.1f}%)"
        )

    if all_pareto:
        combined_pareto = pd.concat(all_pareto, ignore_index=True)
        pareto_all_path = pareto_dir / "pareto_all.csv"
        combined_pareto.to_csv(pareto_all_path, index=False)
        print(f"\nWrote {len(combined_pareto)} Pareto-optimal rows to {pareto_dir}/")

    print(f"\nPareto metrics used (all minimized): {available_metrics}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_PARETO_METRICS = [
    "cacheHitLatency_ns",
    "cacheArea_mm2",
]


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Post-process DESTINY exploration CSVs: add headers, "
            "strip units, combine by capacity, and Pareto-prune."
        )
    )
    parser.add_argument(
        "csv_dir",
        type=Path,
        help="Directory containing raw DESTINY exploration CSVs (e.g., sweep_out/full_csvs/)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: <csv_dir>/../postprocessed/)",
    )
    parser.add_argument(
        "--pareto-metrics",
        nargs="+",
        default=DEFAULT_PARETO_METRICS,
        help=(
            "Columns to use for Pareto pruning (all minimized). "
            f"Default: {DEFAULT_PARETO_METRICS}"
        ),
    )
    parser.add_argument(
        "--no-pareto",
        action="store_true",
        help="Skip Pareto pruning (only combine CSVs)",
    )
    args = parser.parse_args()

    csv_dir = args.csv_dir.resolve()
    out_dir = (args.out or csv_dir.parent / "postprocessed").resolve()

    metrics = [] if args.no_pareto else args.pareto_metrics

    process_csvs(csv_dir, out_dir, metrics)


if __name__ == "__main__":
    main()
