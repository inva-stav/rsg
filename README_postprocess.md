# postprocess_destiny.py

Unified post-processing for DESTINY exploration CSVs produced by `sweep_destiny`.

Replaces both `add_destiny_csv_headers.sh` and `pareto_destiny.py` with a single script.

## What it does

1. **Adds column headers** — auto-detects cache-mode (94 cols) vs memory-mode (40 cols)
2. **Strips unit suffixes** — converts values like `"123.45pW"` to plain numbers in the canonical unit indicated by the column name (e.g., `leakage_mW` → milliwatts)
3. **Adds metadata columns** — extracts `_capacity`, `_cell_type`, `_opt_target`, `_template` from each CSV filename
4. **Combines per-capacity** — all 64KB designs in one file, all 128KB in another, etc., with `_cell_type` identifying the memory technology
5. **Pareto prunes** — filters each capacity group to its Pareto frontier using `paretoset`

## Installation

Place `postprocess_destiny.py` in the `sweeps/` directory (alongside `sweep_destiny.cpp`).

Install Python dependencies:

```bash
pip install pandas paretoset
```

## Usage

Run from anywhere, pointing at the `full_csvs/` directory produced by the sweep:

```bash
# Basic usage (combines + Pareto prunes on default metrics)
python sweeps/postprocess_destiny.py config/sweep_out/full_csvs/

# Custom output directory
python sweeps/postprocess_destiny.py config/sweep_out/full_csvs/ --out results/

# Skip Pareto pruning (only combine)
python sweeps/postprocess_destiny.py config/sweep_out/full_csvs/ --no-pareto

# Custom Pareto metrics
python sweeps/postprocess_destiny.py config/sweep_out/full_csvs/ \
    --pareto-metrics cacheHitLatency_ns cacheWriteLatency_ns cacheLeakage_mW cacheArea_mm2
```

## Output

```
postprocessed/
  combined/
    combined_32KB.csv       # all memory types for 32KB
    combined_64KB.csv
    combined_128KB.csv
    ...
    combined_all.csv        # everything in one file
  pareto/
    pareto_32KB.csv         # Pareto frontier for 32KB
    pareto_64KB.csv
    ...
    pareto_all.csv          # all Pareto-optimal rows
```

### Metadata columns added to every row

| Column | Example | Source |
|--------|---------|--------|
| `_source_file` | `tmp__sample_2D_eDRAM__eDRAM_2D__128KB__Full.csv` | Original filename |
| `_capacity` | `128KB` | Extracted from filename |
| `_cell_type` | `eDRAM_2D` | Extracted from filename (the `.cell` name without extension) |
| `_opt_target` | `Full` | Extracted from filename |
| `_template` | `sample_2D_eDRAM` | Extracted from filename |

## Default Pareto metrics

All minimized:
- `cacheHitLatency_ns`
- `cacheLeakage_mW`
- `cacheArea_mm2`

Override with `--pareto-metrics`.

## Unit conversion reference

Values with embedded units (e.g., `"636.852mW"`, `"0.134nJ"`) are converted to the unit in the column name:

| Column suffix | Target unit | Recognized input units |
|---------------|-------------|----------------------|
| `_ns` | nanoseconds | ps, ns, us, ms, s |
| `_pJ` | picojoules | pJ, nJ, uJ, mJ, J |
| `_nJ` | nanojoules | pJ, nJ, uJ, mJ, J |
| `_mW` | milliwatts | pW, nW, uW, mW, W |
| `_W` | watts | pW, nW, uW, mW, W |
| `_um` | micrometers | nm, um, mm |
| `_um2` / `_mm2` | area | um2, um^2, mm2, mm^2 |
| `_pct` | percent | % |

## Workflow (complete)

```bash
# 1. Build & run the sweep (unchanged)
cd destiny_3d_cache/config
g++ -std=gnu++17 -O2 -Wall ../../sweeps/sweep_destiny.cpp -o sweep_destiny
./sweep_destiny

# 2. Post-process (replaces add_destiny_csv_headers.sh + pareto_destiny.py)
python ../../sweeps/postprocess_destiny.py sweep_out/full_csvs/

# 3. Results are in sweep_out/postprocessed/
ls sweep_out/postprocessed/combined/
ls sweep_out/postprocessed/pareto/
```

## Migration from old scripts

This script replaces:
- `add_destiny_csv_headers.sh` — header addition is now automatic
- `pareto_destiny.py` — Pareto pruning is integrated

You can safely remove both old scripts after switching to `postprocess_destiny.py`.
