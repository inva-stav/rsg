# DESTINY Sweeper (Design Space Exploration Runner)

This project contains a sweep driver (`sweep_destiny.cpp`) that automates running **DESTINY** across many configurations and collecting results into a single CSV, while saving raw logs and (optionally) full design-space CSV outputs produced by DESTINY.

The goal is to make large parameter sweeps reproducible:
- Generate config variants from template `.cfg` files
- Run DESTINY for each variant
- Parse key metrics from stdout
- Write an aggregated results file for downstream analysis (e.g., Pareto frontiers)

---

## What the Sweeper Does

For each template config listed in `CFG_TEMPLATES` in `sweep_destiny.cpp`, the sweeper:

1. **Reads template metadata**
   - Capacity unit (KB vs MB)
   - `-MemoryCellInputFile`
   - Other parameters remain as provided by the template

2. **Generates temporary configs**
   - Edits `-Capacity (KB|MB): ...`
   - Optionally edits `-MemoryCellInputFile: ...` (if `SWAP_CELL_FILES=true`)
   - Optionally edits `-OptimizationTarget: ...` (if `SWEEP_OPT_TARGETS=true`)

3. **Runs DESTINY**
   - Executes `../destiny <relative_path_to_tmp_cfg>`
   - Captures combined stdout/stderr for each run

4. **Captures artifacts**
   - Raw run logs: `config/sweep_out/logs/<tmp_cfg>.txt`
   - Temporary cfgs: `config/sweep_out/tmp_cfgs/<tmp_cfg>.cfg`
   - DESTINY-produced exploration CSVs: copied into `config/sweep_out/full_csvs/` when present
   - Aggregated metrics CSV: `config/sweep_out/destiny_sweep_results.csv`

---

## Output Layout

All outputs land under:
destiny_3d_cache/config/sweep_out/
├── destiny_sweep_results.csv # aggregated results across runs
├── logs/ # raw stdout logs per run
├── tmp_cfgs/ # generated cfgs per run
└── full_csvs/ # copied DESTINY full exploration CSVs (if produced)


### Aggregated CSV Contents

`destiny_sweep_results.csv` includes one row per run, with columns such as:

- template cfg name / generated cfg name
- capacity value + units
- cell file specified vs cell file actually used
- design target / optimized-for metadata (from DESTINY output)
- parsed metrics:
  - read latency (ns), write latency (ns)
  - area (mm^2)
  - read/write dynamic energy (pJ)
  - leakage power (uW)
- status, return code, log file path
- optional `exploration_csv` path if a full exploration CSV was produced

---

## Reproducing the Results (End-to-End)

### 0) Prerequisites

You need:
- A working **Linux** environment (e.g., the RSG VM)
- A **Linux-built** DESTINY binary at:
destiny_3d_cache/destiny
- Template cfgs and `.cell` files in:
destiny_3d_cache/config/

> If you see `Exec format error`, your `destiny` binary was built for the wrong architecture (common if built on macOS). Rebuild DESTINY on the VM.

### 1) Build DESTINY (if needed)

From `destiny_3d_cache/` (exact commands depend on the project’s build system):
- Either run the provided build script / Makefile if available
- Or build using the project’s documented instructions

Verify:
```bash
file destiny
# should say: ELF 64-bit LSB executable ... x86-64 (or appropriate Linux arch)

**### 2) Build the Sweeper**

From destiny_3d_cache/config/:
g++ -std=gnu++17 -O2 -Wall sweep_destiny.cpp -o sweep_destiny
If your environment requires it (older GCC toolchains):
g++ -std=gnu++17 -O2 -Wall sweep_destiny.cpp -o sweep_destiny -lstdc++fs

**3) Configure the Sweep**
Open sweep_destiny.cpp and edit the USER SETTINGS section:

CFG_TEMPLATES: which templates to use
CAPACITIES_KB, CAPACITIES_MB: what capacity sweep to run
SWEEP_OPT_TARGETS + OPT_TARGETS: optimization target sweep
SWAP_CELL_FILES: whether to also sweep across .cell technologies
MAX_FAILURES: stop early after repeated failures (useful during testing)

**4) Run the Sweep**
Important: run from config/ so DESTINY resolves .cell files correctly.
cd destiny_3d_cache/config
./sweep_destiny
ls sweep_out/
