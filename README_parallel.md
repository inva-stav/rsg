# DESTINY Parallel Sweep

`parallel_sweep.py` is a drop-in alternative to `run_pipeline.py` that runs
each memory technology template in its own subprocess, cutting wall-clock time
by roughly the number of templates (e.g. 8 templates → ~8× faster).

---

## Quick Start

```bash
# Full run — compile, sweep all templates in parallel, Pareto-prune
python parallel_sweep.py

# Cap parallelism (useful if CPU/memory is limited)
python parallel_sweep.py --workers 4

# Skip recompile (binary already built)
python parallel_sweep.py --skip-build

# Skip Pareto pruning — just collect and combine CSVs
python parallel_sweep.py --no-pareto

# Custom config file
python parallel_sweep.py --config my_sweep.json
```

---

## Prerequisites

- Same as the main `README.md` (Linux, DESTINY binary, GCC C++17)
- Python 3.9+
- `pandas` and `paretoset` packages (same as `run_pipeline.py`)

---

## Configuration

Uses the same `pipeline_config.json` format as `run_pipeline.py`:

```json
{
  "sweep": {
    "cfg_templates": ["sample_2D_eDRAM.cfg", "sample_3D_eDRAM.cfg", ...],
    "capacities_kb": [32, 64, 128, 256, 512, 1024],
    "capacities_mb": [1, 2, 4, 8],
    "sweep_opt_targets": true,
    "opt_targets": ["WriteEDP", "ReadLatency", "WriteLatency",
                    "ReadDynamicEnergy", "WriteDynamicEnergy", "Full"],
    "swap_cell_files": false,
    "cell_files_to_try": [],
    "max_failures": 10
  },
  "postprocess": {
    "pareto_metrics": ["cacheHitLatency_ns", "cacheLeakage_mW", "cacheArea_mm2"]
  }
}
```

No changes to the config format are needed — the parallel runner reads it
identically.

---

## How It Works

### Step 1 — Build (once)

`sweep_destiny.cpp` is patched with shared settings from the config (opt
targets, cell file swap, max failures) and compiled to a single binary in
`destiny_3d_cache/config/`. This happens once regardless of how many workers
will run.

### Step 2 — Parallel sweep

One subprocess is spawned per template using
`concurrent.futures.ProcessPoolExecutor`. Each worker calls the binary with
its assigned template and the full set of capacities:

```
./sweep_destiny <config_dir> \
    --templates sample_2D_eDRAM.cfg \
    --capacities-kb 32,64,128,256,512,1024 \
    --capacities-mb 1,2,4,8 \
    --out-dir worker_0
```

Workers write to isolated `worker_N/` subdirectories so they never conflict
with each other, even when running simultaneously.

### Step 3 — Merge + postprocess

Once all workers finish, their `full_csvs/*.csv` files are copied into a
single shared `sweep_out/full_csvs/` directory (filenames are unique across
workers because they embed the template stem). `postprocess_destiny` then runs
Pareto pruning on the merged output exactly as it would in the sequential
pipeline.

---

## Output Layout

```text
destiny_3d_cache/config/
├── sweep_destiny               # compiled binary
├── worker_0/                   # eDRAM 2D intermediate outputs
│   ├── full_csvs/
│   ├── logs/
│   └── tmp_cfgs/
├── worker_1/                   # eDRAM 3D intermediate outputs
│   └── ...
├── ...
└── sweep_out/
    ├── full_csvs/              # merged exploration CSVs from all workers
    └── postprocessed/          # Pareto-pruned final results
```

The `sweep_out/` layout matches what `run_pipeline.py` produces, so downstream
analysis scripts work with either runner.

---

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--config FILE` | `pipeline_config.json` | Pipeline config JSON |
| `--config-dir PATH` | `destiny_3d_cache/config/` | DESTINY config directory |
| `--workers N` | one per template | Max parallel subprocesses |
| `--skip-build` | off | Reuse existing binary, skip compilation |
| `--no-pareto` | off | Skip Pareto pruning |
| `--pareto-metrics M [M ...]` | from config | Columns to minimize in Pareto filter |

---

## C++ CLI Flags (sweep_destiny)

The sweeper binary now accepts runtime overrides so the Python driver can
partition work without recompiling:

| Flag | Example | Description |
|------|---------|-------------|
| `--templates` | `sample_2D_eDRAM.cfg` | Comma-separated templates to process |
| `--capacities-kb` | `32,64,128,256,512,1024` | KB capacity values |
| `--capacities-mb` | `1,2,4,8` | MB capacity values |
| `--out-dir` | `worker_0` | Output subdirectory (relative to config dir) |

When any flag is absent the binary falls back to its compile-time static
values, so existing invocations (`./sweep_destiny` with no flags) are
unchanged.

---

## Comparison with run_pipeline.py

| | `run_pipeline.py` | `parallel_sweep.py` |
|---|---|---|
| Parallelism | None — sequential | One process per template |
| Expected speedup | 1× | ~N× (N = templates) |
| Config format | `pipeline_config.json` | Same |
| Output location | `sweep_out/` | Same (after merge) |
| Best for | Quick single-tech runs, debugging | Full multi-tech sweeps |

Both scripts are fully compatible — you can run one and then use
`--use-existing-csvs` / `--skip-build` on the other.
