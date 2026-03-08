# DESTINY Sweeper  
*A reproducible design-space exploration runner for DESTINY*

This repository contains a sweep driver (`sweep_destiny.cpp`) that automates running **DESTINY** across large parameter spaces and aggregates results into a single CSV. It also preserves raw logs and (optionally) full design-space CSV outputs produced by DESTINY itself.

The primary goal is to make large architectural sweeps **reproducible, auditable, and easy to analyze**.

---

## Features

- Generate configuration variants from template `.cfg` files
- Sweep memory capacity, optimization targets, and memory cell technologies
- Run DESTINY automatically for each configuration
- Parse key metrics directly from DESTINY stdout
- Aggregate results into a single CSV for downstream analysis
- Preserve raw logs, generated configs, and optional full exploration CSVs

---

## Repository Structure

```text
destiny_3d_cache/
├── destiny                     # DESTINY binary (Linux)
├── config/
│   ├── sweep_destiny.cpp       # sweep driver
│   ├── *.cfg                   # template configuration files
│   ├── *.cell                  # memory cell technology files
│   └── sweep_out/              # generated outputs (created at runtime)
│       ├── destiny_sweep_results.csv
│       ├── logs/
│       ├── tmp_cfgs/
│       └── full_csvs/
````

---

## What the Sweeper Does

For each template configuration listed in `CFG_TEMPLATES` inside `sweep_destiny.cpp`, the sweeper:

1. **Reads template metadata**

   * Capacity units (KB vs MB)
   * Memory cell input file
   * All other parameters are preserved from the template

2. **Generates temporary configurations**

   * Updates capacity values
   * Optionally swaps memory cell technology files
   * Optionally sweeps optimization targets

3. **Runs DESTINY**

   * Executes `../destiny <relative_path_to_tmp_cfg>`
   * Captures combined stdout/stderr

4. **Collects artifacts**

   * Raw logs for every run
   * Generated temporary `.cfg` files
   * Optional full design-space CSVs produced by DESTINY
   * A single aggregated CSV with parsed metrics

---

## Output Layout

All outputs are written under:

```text
config/sweep_out/
├── destiny_sweep_results.csv   # aggregated metrics across all runs
├── logs/                       # raw stdout/stderr per run
├── tmp_cfgs/                   # generated configuration files
└── full_csvs/                  # DESTINY exploration CSVs (if produced)
```

---

## Aggregated Results Format

Each row in `destiny_sweep_results.csv` corresponds to **one DESTINY run** and includes:

* Template config name and generated config name
* Capacity value and units
* Memory cell file specified vs. actually used
* Optimization target metadata
* Parsed performance metrics:

  * Read latency (ns)
  * Write latency (ns)
  * Area (mm²)
  * Read/write dynamic energy (pJ)
  * Leakage power (µW)
* Exit status, return code, and log file path
* Optional path to a full exploration CSV (if generated)

This format is designed to be directly usable for:

* Pareto frontier analysis
* Design trade-off visualization
* Batch post-processing in Python, MATLAB, or R

---

## Quick Start

### Prerequisites

* Linux environment
* A **Linux-built** DESTINY binary
* C++17-compatible compiler (GCC recommended)

> **Important**
> If you see `Exec format error`, your DESTINY binary was built for the wrong architecture (e.g., macOS). DESTINY must be built on Linux.

---

### 1) Build DESTINY

Follow the official DESTINY build instructions for your environment.

Verify the binary:

```bash
file destiny
```

You should see output similar to:

```text
ELF 64-bit LSB executable, x86-64
```

---

### 2) Build the Sweeper

From the `config/` directory:

```bash
g++ -std=gnu++17 -O2 -Wall ../../sweeps/sweep_destiny.cpp -o sweep_destiny
```

If required by older toolchains:

```bash
g++ -std=gnu++17 -O2 -Wall sweep_destiny.cpp -o sweep_destiny -lstdc++fs
```

---

### 3) Configure the Sweep

Edit the **USER SETTINGS** section of `sweep_destiny.cpp`:

* `CFG_TEMPLATES` — template `.cfg` files to sweep
* `CAPACITIES_KB`, `CAPACITIES_MB` — capacity values
* `SWEEP_OPT_TARGETS`, `OPT_TARGETS` — optimization target sweep
* `SWAP_CELL_FILES` — enable sweeping across `.cell` technologies
* `MAX_FAILURES` — abort after repeated failures (useful for testing)

---

### 4) Run

Run the sweeper **from the `config/` directory** so DESTINY resolves relative paths correctly:

```bash
cd destiny_3d_cache/config
./sweep_destiny
```

Results will appear in:

```bash
ls sweep_out/
```

---

## Design Notes

* The sweeper assumes DESTINY resolves `.cell` files relative to the working directory.
* All generated files are kept for reproducibility and debugging.
* Failures are recorded in the aggregated CSV rather than silently skipped.
* The driver is intentionally implemented as a single self-contained C++ file to simplify deployment on remote clusters or VMs.

---

## Limitations

* This tool does not modify DESTINY internals.
* Parsing relies on stable DESTINY stdout formatting.
* Parallel execution across memory technologies is available via `parallel_sweep.py` — see [`README_parallel.md`](README_parallel.md).

---

## License

This repository contains **only the sweep driver**.
DESTINY itself is subject to its own license and is **not redistributed** here.