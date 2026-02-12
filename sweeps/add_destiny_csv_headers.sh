#!/usr/bin/env bash
# add_destiny_csv_headers.sh
#
# Prepends column headers to Destiny exploration CSV files that lack them.
# Handles both cache-mode CSVs (94 cols) and memory-mode CSVs (40 cols).
#
# Usage:
#   ./add_destiny_csv_headers.sh <dir_or_file> [<dir_or_file> ...]
#
# Examples:
#   ./add_destiny_csv_headers.sh path/to/full_csvs/
#   ./add_destiny_csv_headers.sh file1.csv file2.csv

set -euo pipefail

# -- Bank-level columns (40), written by Result::printToCsvFile --
bank_columns() {
    local p="${1:-}"  # optional prefix like "data_" or "tag_"
    echo -n "${p}numRowMat,${p}numColumnMat,${p}stackedDieCount,${p}numActiveMatPerColumn,${p}numActiveMatPerRow,"
    echo -n "${p}numRowSubarray,${p}numColumnSubarray,${p}numActiveSubarrayPerColumn,${p}numActiveSubarrayPerRow,"
    echo -n "${p}numRow_subarray,${p}numColumn_subarray,"
    echo -n "${p}muxSenseAmp,${p}muxOutputLev1,${p}muxOutputLev2,${p}numRowPerSet,"
    echo -n "${p}localWireType,${p}localWireRepeaterType,${p}localWireLowSwing,"
    echo -n "${p}globalWireType,${p}globalWireRepeaterType,${p}globalWireLowSwing,"
    echo -n "${p}bufferDesignStyle,"
    echo -n "${p}bankHeight_um,${p}bankWidth_um,${p}bankArea_um2,"
    echo -n "${p}matHeight_um,${p}matWidth_um,${p}matArea_um2,"
    echo -n "${p}subarrayHeight_um,${p}subarrayWidth_um,${p}subarrayArea_um2,"
    echo -n "${p}areaEfficiency_pct,"
    echo -n "${p}readLatency_ns,${p}writeLatency_ns,${p}refreshLatency_ns,"
    echo -n "${p}readDynamicEnergy_pJ,${p}writeDynamicEnergy_pJ,${p}refreshDynamicEnergy_pJ,"
    echo -n "${p}leakage_mW,${p}refreshPower_W,"
}

# -- Cache header (94 cols): cache-level + data bank + tag bank + combined --
cache_header() {
    echo -n "cacheAccessMode,cacheArea_mm2,"
    echo -n "cacheHitLatency_ns,cacheMissLatency_ns,cacheWriteLatency_ns,cacheRefreshLatency_ns,"
    echo -n "cacheHitDynamicEnergy_nJ,cacheMissDynamicEnergy_nJ,cacheWriteDynamicEnergy_nJ,cacheRefreshDynamicEnergy_nJ,"
    echo -n "cacheLeakage_mW,cacheRefreshPower_W,"
    bank_columns "data_"
    bank_columns "tag_"
    echo -n "combinedSubarrayLeakage,combinedSubarrayArea_mm2,"
}

# -- Memory (non-cache) header (40 cols) --
memory_header() {
    bank_columns ""
}

# Detect whether a CSV is cache-mode by checking if the first field of the
# first data row is a known cache access mode string.
is_cache_csv() {
    local first_field
    first_field=$(head -1 "$1" | cut -d',' -f1)
    case "$first_field" in
        Normal|Fast|Sequential) return 0 ;;
        *) return 1 ;;
    esac
}

# Check if file already has a header (first field is a known header name)
already_has_header() {
    local first_field
    first_field=$(head -1 "$1" | cut -d',' -f1)
    case "$first_field" in
        cacheAccessMode|numRowMat) return 0 ;;
        *) return 1 ;;
    esac
}

add_header() {
    local csv="$1"

    if [ ! -f "$csv" ]; then
        echo "[WARN] Not a file: $csv" >&2
        return
    fi

    if already_has_header "$csv"; then
        echo "[SKIP] Already has header: $csv"
        return
    fi

    local header
    if is_cache_csv "$csv"; then
        header=$(cache_header)
    else
        header=$(memory_header)
    fi

    # Prepend header using a temp file
    local tmp
    tmp=$(mktemp "${csv}.tmp.XXXXXX")
    { echo "$header"; cat "$csv"; } > "$tmp"
    mv "$tmp" "$csv"
    echo "[OK]   Added header: $csv"
}

# --- Main ---
if [ $# -eq 0 ]; then
    echo "Usage: $0 <dir_or_file> [<dir_or_file> ...]" >&2
    exit 1
fi

for arg in "$@"; do
    if [ -d "$arg" ]; then
        for csv in "$arg"/*.csv; do
            [ -f "$csv" ] && add_header "$csv"
        done
    else
        add_header "$arg"
    fi
done
