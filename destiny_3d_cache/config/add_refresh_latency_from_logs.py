import csv
import os
import re
from pathlib import Path

# --------- SETTINGS (edit if needed) ----------
OUT_DIR = Path("sweep_out")
INPUT_CSV = OUT_DIR / "destiny_sweep_results.csv"
OUTPUT_CSV = OUT_DIR / "destiny_sweep_results_with_refresh.csv"
# ---------------------------------------------

CACHE_REFRESH_RE = re.compile(
    r"^\s*-\s*Cache Refresh Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\b",
    re.IGNORECASE
)

ARRAY_REFRESH_RE = re.compile(
    r"^\s*-\s*Refresh Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\s*$",
    re.IGNORECASE
)

def time_to_ns(val: float, unit: str) -> float:
    u = unit.lower()
    if u == "ps": return val * 1e-3
    if u == "ns": return val
    if u == "us": return val * 1e3
    if u == "ms": return val * 1e6
    if u == "s":  return val * 1e9
    raise ValueError(f"Unknown time unit: {unit}")

def extract_refresh_latency_ns(log_text: str):
    # Prefer cache summary refresh latency
    for line in log_text.splitlines():
        m = CACHE_REFRESH_RE.match(line)
        if m:
            return time_to_ns(float(m.group(1)), m.group(2))

    # Fallback: array-level refresh latency
    for line in log_text.splitlines():
        m = ARRAY_REFRESH_RE.match(line)
        if m:
            return time_to_ns(float(m.group(1)), m.group(2))

    return None

def main():
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"Input CSV not found: {INPUT_CSV}")

    rows_out = []
    missing_logs = 0
    missing_refresh = 0

    with INPUT_CSV.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])

        if "log_file" not in fieldnames:
            raise ValueError("CSV must contain a 'log_file' column so we can locate each log.")

        # Add new column if not present
        new_col = "refresh_latency_ns"
        if new_col not in fieldnames:
            fieldnames.append(new_col)

        for row in reader:
            log_rel = row.get("log_file", "").strip()
            refresh_ns = None

            if log_rel:
                log_path = Path(log_rel)
                if not log_path.is_absolute():
                    log_path = Path(".") / log_path  # relative to config/

                if log_path.exists():
                    text = log_path.read_text(errors="replace")
                    refresh_ns = extract_refresh_latency_ns(text)
                    if refresh_ns is None:
                        missing_refresh += 1
                else:
                    missing_logs += 1
            else:
                missing_logs += 1

            row[new_col] = "" if refresh_ns is None else f"{refresh_ns}"
            rows_out.append(row)

    with OUTPUT_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"Wrote: {OUTPUT_CSV}")
    print(f"Rows: {len(rows_out)}")
    print(f"Missing log files: {missing_logs}")
    print(f"Logs without refresh latency found: {missing_refresh}")

if __name__ == "__main__":
    main()
