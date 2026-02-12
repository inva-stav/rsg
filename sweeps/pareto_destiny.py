#!/usr/bin/env python3
"""
pareto_destiny.py

Aggregates Destiny exploration CSV results from full_csvs/ by memory capacity,
then prunes each capacity group to its Pareto frontier for a configurable set
of metrics (all minimized).

Usage:
    python pareto_destiny.py <full_csvs_dir> [--out <output_dir>]

Outputs one CSV per capacity: pareto_<capacity>.csv
Also outputs a combined CSV: pareto_all.csv
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
from paretoset import paretoset

# ---------------------------------------------------------------------------
# Metrics to use for Pareto pruning (all minimized)
# ---------------------------------------------------------------------------
PARETO_METRICS = [
    "cacheHitLatency_ns",
    #"cacheWriteLatency_ns",
    #"cacheHitDynamicEnergy_nJ",
    #"cacheLeakage_mW",
    #"cacheArea_mm2",
]


def extract_capacity(filename: str) -> str | None:
    """Extract capacity string (e.g. '128KB') from a Destiny CSV filename.

    Expected pattern: tmp__<template>__<cell>__<capacity>__<opt_target>.csv
    """
    m = re.search(r"__(\d+(?:\.\d+)?[KMG]B)__", filename)
    return m.group(1) if m else None


def load_csvs(csv_dir: Path) -> pd.DataFrame:
    """Load all CSVs in a directory, tagging each row with source file and capacity."""
    frames = []
    for csv_path in sorted(csv_dir.glob("*.csv")):
        cap = extract_capacity(csv_path.name)
        if cap is None:
            print(f"[WARN] Cannot parse capacity from filename: {csv_path.name}", file=sys.stderr)
            continue

        df = pd.read_csv(csv_path)

        # Drop the empty trailing column produced by Destiny's trailing comma
        if df.columns[-1].startswith("Unnamed"):
            df = df.drop(columns=[df.columns[-1]])

        df["_source_file"] = csv_path.name
        df["_capacity"] = cap
        frames.append(df)

    if not frames:
        print("[ERROR] No CSV files found.", file=sys.stderr)
        sys.exit(1)

    return pd.concat(frames, ignore_index=True)


def capacity_sort_key(cap: str) -> float:
    """Convert capacity string to numeric KB for sorting."""
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


def main():
    parser = argparse.ArgumentParser(description="Pareto-prune Destiny exploration CSVs by capacity")
    parser.add_argument("csv_dir", type=Path, help="Directory containing the full_csvs exploration files")
    parser.add_argument("--out", type=Path, default=None, help="Output directory (default: <csv_dir>/pareto/)")
    args = parser.parse_args()

    csv_dir = args.csv_dir
    out_dir = args.out or csv_dir / "pareto"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load and aggregate
    df = load_csvs(csv_dir)
    print(f"Loaded {len(df)} total rows from {df['_source_file'].nunique()} files")

    # Verify that all metrics exist
    missing = [m for m in PARETO_METRICS if m not in df.columns]
    if missing:
        print(f"[ERROR] Missing columns: {missing}", file=sys.stderr)
        print(f"  Available columns: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    # Coerce metric columns to numeric (non-numeric → NaN → drop)
    for m in PARETO_METRICS:
        df[m] = pd.to_numeric(df[m], errors="coerce")
    before = len(df)
    df = df.dropna(subset=PARETO_METRICS)
    if len(df) < before:
        print(f"  Dropped {before - len(df)} rows with non-numeric metric values")

    # Process each capacity group
    all_pareto = []
    capacities = sorted(df["_capacity"].unique(), key=capacity_sort_key)

    for cap in capacities:
        group = df[df["_capacity"] == cap].copy()
        costs = group[PARETO_METRICS].values
        mask = paretoset(costs, sense=["min"] * len(PARETO_METRICS))
        pareto = group[mask].copy()

        # Write per-capacity file
        out_path = out_dir / f"pareto_{cap}.csv"
        pareto.to_csv(out_path, index=False)

        all_pareto.append(pareto)
        print(f"  {cap:>8s}: {len(group):6d} candidates → {len(pareto):5d} Pareto-optimal ({len(pareto)/len(group)*100:.1f}%)")

    # Write combined file
    combined = pd.concat(all_pareto, ignore_index=True)
    combined_path = out_dir / "pareto_all.csv"
    combined.to_csv(combined_path, index=False)

    print(f"\nWrote {len(combined)} total Pareto-optimal rows to {out_dir}/")
    print(f"  Per-capacity files: pareto_<capacity>.csv")
    print(f"  Combined file:      pareto_all.csv")
    print(f"\nMetrics used (all minimized): {PARETO_METRICS}")


if __name__ == "__main__":
    main()
