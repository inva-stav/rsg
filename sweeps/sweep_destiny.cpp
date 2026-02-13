// sweep_destiny.cpp
//
// Build (macOS / Linux):
//   g++ -std=c++17 -O2 -Wall sweep_destiny.cpp -o sweep_destiny
//
// Run (IMPORTANT: run from config/ folder):
//   ./sweep_destiny
//
// What it does:
// - Uses your sample_*.cfg files as templates
// - Creates temp cfgs per (template × capacity × [optional cell swap])
// - Runs ../destiny <temp_cfg>
// - Parses stdout for:
//     Read Latency (ns), Write Latency (ns), Total Area (mm^2),
//     Read Dynamic Energy (pJ), Write Dynamic Energy (pJ),
//     Leakage Power (uW)
// - Writes combined CSV + per-capacity Pareto-filtered CSVs + saves raw logs per run
//   All numeric columns are unit-free; units documented in column names:
//     read_latency(ns), write_latency(ns), area(mm2),
//     read_dynamic_energy(pJ), write_dynamic_energy(pJ), leakage_power(uW)
//
// Notes:
// - DESTINY resolves MemoryCellInputFile relative to the working directory,
//   so this program assumes it is executed from the config/ directory.

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <regex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace fs = std::filesystem;

// ----------------------------
// USER SETTINGS (EDIT THESE)
// ----------------------------

// Which cfg templates to sweep (relative to config/)
static const std::vector<std::string> CFG_TEMPLATES = {
    "sample_2D_eDRAM.cfg",
    "sample_3D_eDRAM.cfg",
    "sample_2DReRAM.cfg",
    "sample_3DReRAM.cfg",
    "sample_PCRAM.cfg",
    "sample_STTRAM.cfg",
    "sample_SRAM_2layer.cfg",
    "sample_SRAM_4layer.cfg",
};
// Optimization target sweep
static const bool SWEEP_OPT_TARGETS = true;

// Use these exact strings as DESTINY expects in cfg.
// (You can add/remove; start small if runtime blows up.)
static const std::vector<std::string> OPT_TARGETS = {
    "WriteEDP",
    "ReadLatency",
    "WriteLatency",
    "ReadDynamicEnergy",
    "WriteDynamicEnergy",
    "Full"
};

// Capacity sweep values (preserves unit used by each template)
static const std::vector<double> CAPACITIES_KB = {32, 64, 128, 256, 512, 1024};
static const std::vector<double> CAPACITIES_MB = {1, 2, 4, 8};

// If you ALSO want to sweep technology by swapping MemoryCellInputFile
static const bool SWAP_CELL_FILES = false;

// If SWAP_CELL_FILES=true, list cell files here; if empty, auto-include all *.cell in config/
static const std::vector<std::string> CELL_FILES_TO_TRY = {};

// DESTINY binary path relative to config/
static const std::string DESTINY_REL_PATH = "../destiny";

// Output locations (relative to config/)
static const std::string OUT_DIR = "sweep_out";
static const std::string CSV_NAME = "destiny_sweep_results.csv";

// Safety: stop after N failures? (std::nullopt = never stop)
static const std::optional<int> MAX_FAILURES = 10;

// ----------------------------
// Helpers
// ----------------------------

static std::string read_file(const fs::path& p) {
    std::ifstream in(p, std::ios::in | std::ios::binary);
    if (!in) throw std::runtime_error("Failed to open file: " + p.string());
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

static void write_file(const fs::path& p, const std::string& s) {
    std::ofstream out(p, std::ios::out | std::ios::binary);
    if (!out) throw std::runtime_error("Failed to write file: " + p.string());
    out << s;
}

static std::string trim(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && std::isspace(static_cast<unsigned char>(s[b]))) b++;
    size_t e = s.size();
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) e--;
    return s.substr(b, e - b);
}

static std::vector<std::string> split_lines(const std::string& text) {
    std::vector<std::string> lines;
    std::string line;
    std::istringstream iss(text);
    while (std::getline(iss, line)) lines.push_back(line);
    return lines;
}

static std::string join_lines(const std::vector<std::string>& lines) {
    std::ostringstream oss;
    for (size_t i = 0; i < lines.size(); i++) {
        oss << lines[i];
        if (i + 1 < lines.size()) oss << "\n";
    }
    oss << "\n";
    return oss.str();
}

static std::string shell_escape(const std::string& s) {
    // minimal single-quote escaping for POSIX shells
    std::string out = "'";
    for (char c : s) {
        if (c == '\'') out += "'\\''";
        else out += c;
    }
    out += "'";
    return out;
}

static std::pair<int, std::string> run_command_capture(const std::string& command, const fs::path& cwd) {
    // Run: (cd cwd && command) 2>&1, capture stdout
    // Return: exit code, combined output
    std::string full =
        "cd " + shell_escape(cwd.string()) + " && " + command + " 2>&1";

    FILE* pipe = popen(full.c_str(), "r");
    if (!pipe) {
        throw std::runtime_error("popen failed: " + std::string(std::strerror(errno)));
    }

    std::string output;
    char buffer[4096];
    while (true) {
        size_t n = std::fread(buffer, 1, sizeof(buffer), pipe);
        if (n > 0) output.append(buffer, buffer + n);
        if (n < sizeof(buffer)) {
            if (std::feof(pipe)) break;
            if (std::ferror(pipe)) break;
        }
    }

    int rc = pclose(pipe);
    int exit_code = 0;
#ifdef _WIN32
    exit_code = rc;
#else
    if (WIFEXITED(rc)) exit_code = WEXITSTATUS(rc);
    else exit_code = rc;
#endif
    return {exit_code, output};
}
static double time_to_ns(double val, const std::string& unit_raw) {
    std::string unit = unit_raw;
    std::transform(unit.begin(), unit.end(), unit.begin(), ::tolower);
    if (unit == "ps") return val * 1e-3;     // 1 ps = 1e-3 ns
    if (unit == "ns") return val;            // 1 ns
    if (unit == "us") return val * 1e3;      // 1 us = 1e3 ns
    if (unit == "ms") return val * 1e6;      // 1 ms = 1e6 ns
    if (unit == "s")  return val * 1e9;      // 1 s  = 1e9 ns
    throw std::runtime_error("Unknown time unit: " + unit_raw);
}

static double area_to_mm2(double val, const std::string& unit_raw) {
    // Supports "mm^2" and "mm2" as printed in different DESTINY outputs.
    std::string unit = unit_raw;
    std::transform(unit.begin(), unit.end(), unit.begin(), ::tolower);
    if (unit == "mm^2" || unit == "mm2") return val;
    throw std::runtime_error("Unknown area unit: " + unit_raw);
}

static double energy_to_pj(double val, const std::string& unit_raw) {
    std::string unit = unit_raw;
    std::transform(unit.begin(), unit.end(), unit.begin(), ::tolower);
    if (unit == "pj") return val;
    if (unit == "nj") return val * 1e3;
    if (unit == "uj") return val * 1e6;
    if (unit == "mj") return val * 1e9;
    if (unit == "j")  return val * 1e12;
    throw std::runtime_error("Unknown energy unit: " + unit_raw);
}

static double power_to_uw(double val, const std::string& unit_raw) {
    std::string unit = unit_raw;
    std::transform(unit.begin(), unit.end(), unit.begin(), ::tolower);
    if (unit == "pw") return val * 1e-6;
    if (unit == "nw") return val * 1e-3;
    if (unit == "uw") return val;
    if (unit == "mw") return val * 1e3;
    if (unit == "w")  return val * 1e6;
    throw std::runtime_error("Unknown power unit: " + unit_raw);
}

// ----------------------------
// Parsing / editing
// ----------------------------

struct TemplateInfo {
    fs::path template_path;
    std::string capacity_unit; // "KB" or "MB"
    double capacity_value;
    std::string cell_file;
};

static TemplateInfo read_template_info(const fs::path& cfg_path) {
    // Matches exact lines like:
    // -Capacity (KB): 128
    // -MemoryCellInputFile: sample_2D_eDRAM.cell
    std::regex cap_re(R"(^-Capacity\s+\((KB|MB)\):\s*([0-9]+(?:\.[0-9]+)?)\s*$)", std::regex::icase);
    std::regex cell_re(R"(^-MemoryCellInputFile:\s*(\S+)\s*$)", std::regex::icase);

    auto lines = split_lines(read_file(cfg_path));
    std::optional<std::string> cap_unit;
    std::optional<double> cap_val;
    std::optional<std::string> cell;

    for (const auto& raw : lines) {
        std::string line = trim(raw);
        if (line.empty()) continue;
        if (line.rfind("//", 0) == 0) continue;

        std::smatch m;
        if (std::regex_match(line, m, cap_re)) {
            cap_unit = std::string(m[1]).substr(0); // KB/MB (preserve)
            std::transform(cap_unit->begin(), cap_unit->end(), cap_unit->begin(), ::toupper);
            cap_val = std::stod(m[2]);
            continue;
        }
        if (std::regex_match(line, m, cell_re)) {
            cell = m[1];
            continue;
        }
    }

    if (!cap_unit || !cap_val) {
        throw std::runtime_error("Could not find '-Capacity (KB|MB):' in " + cfg_path.filename().string());
    }
    if (!cell) {
        throw std::runtime_error("Could not find '-MemoryCellInputFile:' in " + cfg_path.filename().string());
    }

    return TemplateInfo{cfg_path, *cap_unit, *cap_val, *cell};
}

static std::string edit_cfg_text(
    const std::string& original,
    double new_capacity,
    const std::string& capacity_unit,
    const std::optional<std::string>& new_cell_file,
    const std::optional<std::string>& new_opt_target
) {
    std::regex cap_re(R"(^-Capacity\s+\((KB|MB)\):\s*([0-9]+(?:\.[0-9]+)?)\s*$)", std::regex::icase);
    std::regex cell_re(R"(^-MemoryCellInputFile:\s*(\S+)\s*$)", std::regex::icase);
    std::regex opt_re(R"(^-OptimizationTarget:\s*(\S+)\s*$)", std::regex::icase);
    std::regex prune_re(R"(^-EnablePruning:\s*(\S+)\s*$)", std::regex::icase);

    auto lines = split_lines(original);
    bool did_cap = false;
    bool did_cell = false;
    bool did_opt = false;
    bool did_prune = false;

    for (auto& raw : lines) {
        std::string line = trim(raw);
        if (line.empty() || line.rfind("//", 0) == 0) continue;

        std::smatch m;
        if (std::regex_match(line, m, cap_re)) {
            std::string unit = m[1];
            std::transform(unit.begin(), unit.end(), unit.begin(), ::toupper);
            if (unit == capacity_unit) {
                std::ostringstream oss;
                oss << "-Capacity (" << capacity_unit << "): " << new_capacity;
                raw = oss.str();
                did_cap = true;
            }
            continue;
        }
        if (std::regex_match(line, m, cell_re)) {
            if (new_cell_file.has_value()) {
                raw = "-MemoryCellInputFile: " + *new_cell_file;
                did_cell = true;
            }
            continue;
        }

        if (std::regex_match(line, m, opt_re)) {
            if (new_opt_target.has_value()) {
                raw = "-OptimizationTarget: " + *new_opt_target;
            }
            did_opt = true;
            continue;
        }

        if (std::regex_match(line, m, prune_re)) {
            raw = "-EnablePruning: No";
            did_prune = true;
            continue;
        }

    }

    if (!did_cap) {
        throw std::runtime_error("Failed to replace Capacity (" + capacity_unit + ") line in cfg text");
    }
    if (new_cell_file.has_value() && !did_cell) {
        throw std::runtime_error("Failed to replace MemoryCellInputFile line in cfg text");
    }

    if (!did_opt) {
        lines.push_back("-OptimizationTarget: " +
                         (new_opt_target.has_value() ? *new_opt_target : std::string("Full")));
    }
    if (!did_prune) {
        lines.push_back("-EnablePruning: No");
    }


    return join_lines(lines);
}

struct ParsedMetrics {
    std::optional<std::string> design_target;
    std::optional<std::string> optimized_for;
    std::optional<std::string> using_cell_file;
    std::optional<double> read_latency_ns;
    std::optional<double> write_latency_ns;
    std::optional<double> area_mm2;
    std::optional<double> read_dynamic_energy_pj;
    std::optional<double> write_dynamic_energy_pj;
    std::optional<double> leakage_power_uw;
    bool finished = false;
};


static ParsedMetrics parse_stdout(const std::string& out) {
    // Cache summary regexes
    // Examples:
    //  - Total Area = 0.565mm^2
    //  - Cache Hit Latency   = 2.707ns
    //  - Cache Write Latency = 151.357ns
    //  - Cache Hit Dynamic Energy   = 0.134nJ per access
    //  - Cache Write Dynamic Energy = 4.799nJ per access
    //  - Cache Total Leakage Power  = 636.852mW

    std::regex cache_total_area_re(
        R"(^\s*-\s*Total Area\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(mm\^2|mm2)\s*$)",
        std::regex::icase);

    std::regex cache_hit_lat_re(
        R"(^\s*-\s*Cache Hit Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\s*$)",
        std::regex::icase);

    std::regex cache_write_lat_re(
        R"(^\s*-\s*Cache Write Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\s*$)",
        std::regex::icase);

    std::regex cache_hit_energy_re(
        R"(^\s*-\s*Cache Hit Dynamic Energy\s*=\s*([\-0-9]+(?:\.[0-9]+)?)\s*(pJ|nJ|uJ|mJ|J)\s*(?:per\s+access)?\s*$)",
        std::regex::icase);

    std::regex cache_write_energy_re(
        R"(^\s*-\s*Cache Write Dynamic Energy\s*=\s*([\-0-9]+(?:\.[0-9]+)?)\s*(pJ|nJ|uJ|mJ|J)\s*(?:per\s+access)?\s*$)",
        std::regex::icase);

    std::regex cache_total_leak_re(
        R"(^\s*-\s*Cache Total Leakage Power\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(pW|nW|uW|mW|W)\s*$)",
        std::regex::icase);

    // General / fallback regexes (array-level / RAM-style)
    // Updated to accept time units beyond ns, and more area formats.
    std::regex read_lat_re(
        R"(^\s*-\s*Read Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\s*$)",
        std::regex::icase);

    std::regex write_lat_re(
        R"(^\s*-\s*Write Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\s*$)",
        std::regex::icase);

    // Some NVMs print write as SET Latency; treat SET as "write time" fallback
    std::regex set_lat_re(
        R"(^\s*-\s*SET Latency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(ps|ns|us|ms|s)\s*$)",
        std::regex::icase);

    // Area variants:
    //  1) "- Total Area = 1.344mm x 1.581mm = 2.125mm^2"
    //  2) "- Total Area = 0.565mm^2"
    std::regex area_full_re(
        R"(^\s*-\s*Total Area\s*=\s*.*=\s*([0-9]+(?:\.[0-9]+)?)\s*(mm\^2|mm2)\s*$)",
        std::regex::icase);

    std::regex area_short_re(
        R"(^\s*-\s*Total Area\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(mm\^2|mm2)\s*$)",
        std::regex::icase);

    std::regex read_en_re(
        R"(^\s*-\s*Read Dynamic Energy\s*=\s*([\-0-9]+(?:\.[0-9]+)?)\s*(pJ|nJ|uJ|mJ|J)\s*$)",
        std::regex::icase);

    std::regex write_en_re(
        R"(^\s*-\s*Write Dynamic Energy\s*=\s*([\-0-9]+(?:\.[0-9]+)?)\s*(pJ|nJ|uJ|mJ|J)\s*$)",
        std::regex::icase);

    // Some NVMs print write energy as SET Dynamic Energy; treat SET as "write energy" fallback
    std::regex set_en_re(
        R"(^\s*-\s*SET Dynamic Energy\s*=\s*([\-0-9]+(?:\.[0-9]+)?)\s*(pJ|nJ|uJ|mJ|J)\s*$)",
        std::regex::icase);

    std::regex leak_p_re(
        R"(^\s*-\s*Leakage Power\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(pW|nW|uW|mW|W)\s*$)",
        std::regex::icase);

    std::regex finished_re(R"(^\s*Finished!\s*$)", std::regex::icase);
    std::regex using_cell_re(R"(^\s*Using cell file:\s*(\S+)\s*$)", std::regex::icase);
    std::regex optimized_re(R"(^\s*Optimized for:\s*(.+?)\s*$)", std::regex::icase);
    std::regex design_target_re(R"(^\s*Design Target:\s*(.+?)\s*$)", std::regex::icase);
    std::regex csv_done_re(R"(^\s*.*\.csv\s+generated successfully!\s*$)", std::regex::icase);

    ParsedMetrics pm;
    std::smatch m;

    bool saw_cache_summary_area = false;
    bool saw_cache_summary_timing = false;
    bool saw_cache_summary_energy = false;
    bool saw_cache_summary_leak = false;

    auto lines = split_lines(out);
    for (const auto& line : lines) {
        // Metadata
        if (std::regex_match(line, m, using_cell_re)) {
            pm.using_cell_file = std::string(m[1]);
            continue;
        }
        if (std::regex_match(line, m, optimized_re)) {
            pm.optimized_for = trim(std::string(m[1]));
            continue;
        }
        if (std::regex_match(line, m, design_target_re)) {
            pm.design_target = trim(std::string(m[1]));
            continue;
        }
        if (std::regex_match(line, m, csv_done_re)) {
        pm.finished = true;
        continue;
        }

        // -------------------------
        // Cache summary (preferred)
        // -------------------------
        if (std::regex_match(line, m, cache_total_area_re)) {
            double v = std::stod(m[1]);
            pm.area_mm2 = area_to_mm2(v, m[2]);
            saw_cache_summary_area = true;
            continue;
        }
        if (std::regex_match(line, m, cache_hit_lat_re)) {
            double v = std::stod(m[1]);
            pm.read_latency_ns = time_to_ns(v, m[2]);  // HIT = "read access time"
            saw_cache_summary_timing = true;
            continue;
        }
        if (std::regex_match(line, m, cache_write_lat_re)) {
            double v = std::stod(m[1]);
            pm.write_latency_ns = time_to_ns(v, m[2]);
            saw_cache_summary_timing = true;
            continue;
        }
        if (std::regex_match(line, m, cache_hit_energy_re)) {
            double v = std::stod(m[1]);
            pm.read_dynamic_energy_pj = energy_to_pj(v, m[2]); // HIT energy = "read access energy"
            saw_cache_summary_energy = true;
            continue;
        }
        if (std::regex_match(line, m, cache_write_energy_re)) {
            double v = std::stod(m[1]);
            pm.write_dynamic_energy_pj = energy_to_pj(v, m[2]);
            saw_cache_summary_energy = true;
            continue;
        }
        if (std::regex_match(line, m, cache_total_leak_re)) {
            double v = std::stod(m[1]);
            pm.leakage_power_uw = power_to_uw(v, m[2]);
            saw_cache_summary_leak = true;
            continue;
        }

        // ---------------------------------------
        // Fallback parsing (RAM / array-level)
        // Only fill fields if not already filled
        // ---------------------------------------
        if (!pm.read_latency_ns.has_value() && std::regex_match(line, m, read_lat_re)) {
            pm.read_latency_ns = time_to_ns(std::stod(m[1]), m[2]);
            continue;
        }
        if (!pm.write_latency_ns.has_value() && std::regex_match(line, m, write_lat_re)) {
            pm.write_latency_ns = time_to_ns(std::stod(m[1]), m[2]);
            continue;
        }
        // For PCRAM/etc where write is SET
        if (!pm.write_latency_ns.has_value() && std::regex_match(line, m, set_lat_re)) {
            pm.write_latency_ns = time_to_ns(std::stod(m[1]), m[2]);
            continue;
        }

        if (!pm.area_mm2.has_value()) {
            if (std::regex_match(line, m, area_full_re)) {
                pm.area_mm2 = area_to_mm2(std::stod(m[1]), m[2]);
                continue;
            }
            if (std::regex_match(line, m, area_short_re)) {
                pm.area_mm2 = area_to_mm2(std::stod(m[1]), m[2]);
                continue;
            }
        }

        if (!pm.read_dynamic_energy_pj.has_value() && std::regex_match(line, m, read_en_re)) {
            pm.read_dynamic_energy_pj = energy_to_pj(std::stod(m[1]), m[2]);
            continue;
        }
        if (!pm.write_dynamic_energy_pj.has_value() && std::regex_match(line, m, write_en_re)) {
            pm.write_dynamic_energy_pj = energy_to_pj(std::stod(m[1]), m[2]);
            continue;
        }
        // For PCRAM/etc where write energy is SET
        if (!pm.write_dynamic_energy_pj.has_value() && std::regex_match(line, m, set_en_re)) {
            pm.write_dynamic_energy_pj = energy_to_pj(std::stod(m[1]), m[2]);
            continue;
        }

        if (!pm.leakage_power_uw.has_value() && std::regex_match(line, m, leak_p_re)) {
            pm.leakage_power_uw = power_to_uw(std::stod(m[1]), m[2]);
            continue;
        }
    }

    // If it was a cache run and we saw cache summary at all, we keep whatever we captured there.
    // (This avoids accidentally overwriting cache-level metrics with data/tag array details later.)
    // The "only fill if empty" logic above already protects this, so nothing more is required.

    return pm;
}

// CSV row structure
struct Row {
    std::string template_cfg;
    std::string run_cfg;
    double capacity_value = 0.0;
    std::string capacity_unit;
    std::string cell_file_cfg;
    std::string cell_file_used;
    std::string design_target;
    std::string optimized_for;
    std::string optimization_target_cfg;
    std::string memory_type;

    std::string read_latency_ns;
    std::string write_latency_ns;
    std::string area_mm2;
    std::string read_dynamic_energy_pj;
    std::string write_dynamic_energy_pj;
    std::string leakage_power_uw;
    std::string exploration_csv; // path to DESTINY full exploration csv (if any)

    std::string status;
    int return_code = 0;
    std::string log_file;
};

static std::string opt_to_string(const std::optional<double>& v) {
    if (!v.has_value()) return "";
    std::ostringstream oss;
    oss << *v;
    return oss.str();
}
static std::string opt_to_string(const std::optional<std::string>& v) {
    if (!v.has_value()) return "";
    return *v;
}

// Extract memory technology name from template filename.
// e.g. "sample_2D_eDRAM.cfg" -> "2D_eDRAM", "sample_SRAM_4layer.cfg" -> "SRAM_4layer"
static std::string extract_memory_type(const std::string& template_name) {
    std::string name = template_name;
    if (name.rfind("sample_", 0) == 0) name = name.substr(7);
    auto dot = name.rfind(".cfg");
    if (dot != std::string::npos) name = name.substr(0, dot);
    return name;
}

// Build a human-readable capacity key like "64KB" or "2MB".
static std::string capacity_key(double val, const std::string& unit) {
    int ival = static_cast<int>(val);
    if (static_cast<double>(ival) == val)
        return std::to_string(ival) + unit;
    std::ostringstream oss;
    oss << val << unit;
    return oss.str();
}

// Pareto-front filter (all objectives minimised).
// Returns indices (into `rows`) of non-dominated rows that have complete metrics.
static std::vector<size_t> pareto_front_indices(const std::vector<const Row*>& rows) {
    static constexpr int N_OBJ = 6; // read_lat, write_lat, area, read_en, write_en, leak_pow
    struct Point { size_t idx; double m[N_OBJ]; };

    std::vector<Point> pts;
    for (size_t i = 0; i < rows.size(); i++) {
        const auto& r = *rows[i];
        if (r.status != "ok") continue;
        if (r.read_latency_ns.empty() || r.write_latency_ns.empty() ||
            r.area_mm2.empty() || r.read_dynamic_energy_pj.empty() ||
            r.write_dynamic_energy_pj.empty() || r.leakage_power_uw.empty()) continue;
        pts.push_back({i, {
            std::stod(r.read_latency_ns),
            std::stod(r.write_latency_ns),
            std::stod(r.area_mm2),
            std::stod(r.read_dynamic_energy_pj),
            std::stod(r.write_dynamic_energy_pj),
            std::stod(r.leakage_power_uw)
        }});
    }

    std::vector<size_t> result;
    for (size_t i = 0; i < pts.size(); i++) {
        bool dominated = false;
        for (size_t j = 0; j < pts.size() && !dominated; j++) {
            if (i == j) continue;
            bool all_leq = true, any_lt = false;
            for (int k = 0; k < N_OBJ; k++) {
                if (pts[j].m[k] > pts[i].m[k]) { all_leq = false; break; }
                if (pts[j].m[k] < pts[i].m[k]) any_lt = true;
            }
            if (all_leq && any_lt) dominated = true;
        }
        if (!dominated) result.push_back(pts[i].idx);
    }
    return result;
}

// ----------------------------
// Main
// ----------------------------

int main() {
    try {
        // Must run from config/
        fs::path config_dir = fs::current_path();

        fs::path destiny_path = fs::weakly_canonical(config_dir / DESTINY_REL_PATH);
        if (!fs::exists(destiny_path)) {
            std::cerr << "[ERROR] DESTINY binary not found at: " << destiny_path << "\n"
                      << "Edit DESTINY_REL_PATH in the source.\n";
            return 2;
        }

        fs::path out_dir = config_dir / OUT_DIR;
        fs::path tmp_dir = out_dir / "tmp_cfgs";
        fs::path logs_dir = out_dir / "logs";
        fs::create_directories(tmp_dir);
        fs::create_directories(logs_dir);

        fs::path csv_path = out_dir / CSV_NAME;
        fs::path full_csvs_dir = out_dir / "full_csvs";
        fs::create_directories(full_csvs_dir);

        // Resolve cfg templates that exist
        std::vector<fs::path> templates;
        for (const auto& name : CFG_TEMPLATES) {
            fs::path p = config_dir / name;
            if (fs::exists(p)) templates.push_back(p);
            else std::cerr << "[WARN] Template cfg not found, skipping: " << name << "\n";
        }
        if (templates.empty()) {
            std::cerr << "[ERROR] No cfg templates found. Check CFG_TEMPLATES.\n";
            return 2;
        }

        // Determine cell files for optional swap
        std::vector<std::string> cell_files;
        if (SWAP_CELL_FILES) {
            if (!CELL_FILES_TO_TRY.empty()) {
                cell_files = CELL_FILES_TO_TRY;
            } else {
                for (auto& p : fs::directory_iterator(config_dir)) {
                    if (p.is_regular_file() && p.path().extension() == ".cell") {
                        cell_files.push_back(p.path().filename().string());
                    }
                }
                std::sort(cell_files.begin(), cell_files.end());
            }
            if (cell_files.empty()) {
                std::cerr << "[ERROR] SWAP_CELL_FILES=true but no .cell files found.\n";
                return 2;
            }
        }

        std::vector<Row> rows;
        int failures = 0;
        bool stop_requested = false;

        for (const auto& template_cfg : templates) {
            TemplateInfo info = read_template_info(template_cfg);
            std::string template_text = read_file(template_cfg);

            const std::vector<double>* caps = nullptr;
            if (info.capacity_unit == "KB") caps = &CAPACITIES_KB;
            else if (info.capacity_unit == "MB") caps = &CAPACITIES_MB;
            else {
                std::cerr << "[WARN] Unknown capacity unit in " << template_cfg.filename().string()
                          << ": " << info.capacity_unit << " (skipping)\n";
                continue;
            }

            std::vector<std::string> cells_for_template;
            if (SWAP_CELL_FILES) cells_for_template = cell_files;
            else cells_for_template = {info.cell_file};

            for (const auto& cell : cells_for_template) {
                if (SWAP_CELL_FILES) {
                    if (!fs::exists(config_dir / cell)) {
                        std::cerr << "[WARN] Cell file not found, skipping: " << cell << "\n";
                        continue;
                    }
                }
                
                std::vector<std::string> opt_targets_for_run;
                if (SWEEP_OPT_TARGETS) opt_targets_for_run = OPT_TARGETS;
                else opt_targets_for_run = {""}; // no override


                for (const auto& opt_target : opt_targets_for_run) {
                        for (double cap : *caps) {
                            // Create temp cfg filename
                            std::string cell_tag = cell;
                            if (cell_tag.size() >= 5 && cell_tag.substr(cell_tag.size() - 5) == ".cell") {
                                cell_tag = cell_tag.substr(0, cell_tag.size() - 5);
                            }

                            std::ostringstream tmp_name;
                            tmp_name << "tmp__" << template_cfg.stem().string()
                                    << "__" << cell_tag
                                    << "__" << cap << info.capacity_unit;

                            if (SWEEP_OPT_TARGETS) {
                                tmp_name << "__" << opt_target;
                            }

                            tmp_name << ".cfg";

                            fs::path tmp_cfg_path = tmp_dir / tmp_name.str();

                            // Edit cfg: capacity + optional cell swap + optional opt target
                            std::optional<std::string> new_cell = std::nullopt;
                            if (SWAP_CELL_FILES) new_cell = cell;

                            std::optional<std::string> new_opt = std::nullopt;
                            if (SWEEP_OPT_TARGETS) new_opt = opt_target;

                            std::string edited = edit_cfg_text(template_text, cap, info.capacity_unit, new_cell, new_opt);
                            write_file(tmp_cfg_path, edited);

                            // Run destiny from config/ with cfg basename (so it can find .cell files)
                            // NOTE: we pass just the file name, but it lives in tmp_cfgs/, so we need a relative path from config/.
                            // We will call destiny with that relative path.
                            fs::path rel_cfg = fs::relative(tmp_cfg_path, config_dir);

                            std::string cmd = shell_escape(destiny_path.string()) + " " + shell_escape(rel_cfg.string());
                            auto [rc, out] = run_command_capture(cmd, config_dir);

                            // Save log
                            fs::path log_path = logs_dir / (tmp_cfg_path.filename().string() + ".txt");
                            write_file(log_path, out);

                            ParsedMetrics pm = parse_stdout(out);
                            bool ok = (rc == 0) && pm.finished;

                            if (!ok) failures++;
                            
                            fs::path produced_csv = tmp_cfg_path;
                            produced_csv.replace_extension(".csv");

                            std::string exploration_csv_rel = "";
                            if (fs::exists(produced_csv)) {
                                fs::path dst = full_csvs_dir / produced_csv.filename();
                                std::error_code ec;
                                fs::copy_file(produced_csv, dst,
                                            fs::copy_options::overwrite_existing, ec);
                                if (ec) {
                                    std::cerr << "[WARN] Failed to copy exploration CSV: "
                                            << ec.message() << "\n";
                                } else {
                                    exploration_csv_rel = fs::relative(dst, config_dir).string();
                                }
                            }

                            Row r;
                            r.template_cfg = template_cfg.filename().string();
                            r.run_cfg = rel_cfg.string();
                            r.capacity_value = cap;
                            r.capacity_unit = info.capacity_unit;
                            r.cell_file_cfg = SWAP_CELL_FILES ? cell : info.cell_file;
                            r.cell_file_used = opt_to_string(pm.using_cell_file);
                            r.design_target = opt_to_string(pm.design_target);
                            r.optimized_for = opt_to_string(pm.optimized_for);
                            r.optimization_target_cfg = SWEEP_OPT_TARGETS ? opt_target : "";
                            r.memory_type = extract_memory_type(r.template_cfg);

                            r.read_latency_ns = opt_to_string(pm.read_latency_ns);
                            r.write_latency_ns = opt_to_string(pm.write_latency_ns);
                            r.area_mm2 = opt_to_string(pm.area_mm2);
                            r.read_dynamic_energy_pj = opt_to_string(pm.read_dynamic_energy_pj);
                            r.write_dynamic_energy_pj = opt_to_string(pm.write_dynamic_energy_pj);
                            r.leakage_power_uw = opt_to_string(pm.leakage_power_uw);

                            r.status = ok ? "ok" : "fail";
                            r.return_code = rc;
                            r.log_file = fs::relative(log_path, config_dir).string();
                            r.exploration_csv = exploration_csv_rel;

                            rows.push_back(std::move(r));

                            std::cout << "[" << (ok ? "ok" : "fail") << "] "
                                << template_cfg.filename().string()
                                << " | " << cap << info.capacity_unit
                                << (SWEEP_OPT_TARGETS ? (" | " + opt_target) : "")
                                << " | " << r.cell_file_cfg
                                << " -> log: " << rows.back().log_file << "\n";

                                if (MAX_FAILURES.has_value() && failures >= *MAX_FAILURES) {
                                    std::cerr << "[ERROR] Reached MAX_FAILURES=" << *MAX_FAILURES << ". Stopping.\n";
                                    stop_requested = true;
                                    break; // break out of cap loop
                                }
                            }
                        if (stop_requested) break;
                    }
                    if (stop_requested) break;
                }
                if (stop_requested) break;
            }
        
        // -------------------------------------------------
        // Write CSVs
        // -------------------------------------------------
        if (rows.empty()) {
            std::cerr << "[WARN] No rows produced.\n";
            return 0;
        }

        // RFC 4180 CSV field escaping.
        auto esc = [](const std::string& s) -> std::string {
            bool need = s.find_first_of(",\"\n\r") != std::string::npos;
            if (!need) return s;
            std::string out = "\"";
            for (char c : s) {
                if (c == '"') out += "\"\"";
                else out += c;
            }
            out += "\"";
            return out;
        };

        // Helper: write one row to a stream.
        // Units are purely numeric (no "pW" etc.) — units are documented in column names:
        //   read_latency(ns), write_latency(ns), area(mm2),
        //   read_dynamic_energy(pJ), write_dynamic_energy(pJ), leakage_power(uW)
        auto write_row = [&](std::ostream& os, const Row& r) {
            os << esc(r.memory_type) << ","
               << esc(r.template_cfg) << ","
               << esc(r.run_cfg) << ","
               << r.capacity_value << ","
               << esc(r.capacity_unit) << ","
               << esc(r.cell_file_cfg) << ","
               << esc(r.cell_file_used) << ","
               << esc(r.design_target) << ","
               << esc(r.optimized_for) << ","
               << esc(r.optimization_target_cfg) << ","
               << esc(r.read_latency_ns) << ","
               << esc(r.write_latency_ns) << ","
               << esc(r.area_mm2) << ","
               << esc(r.read_dynamic_energy_pj) << ","
               << esc(r.write_dynamic_energy_pj) << ","
               << esc(r.leakage_power_uw) << ","
               << esc(r.status) << ","
               << r.return_code << ","
               << esc(r.log_file) << ","
               << esc(r.exploration_csv)
               << "\n";
        };

        static const std::string FULL_HEADER =
            "memory_type,template_cfg,run_cfg,capacity_value,capacity_unit,"
            "cell_file_cfg,cell_file_used,design_target,optimized_for,"
            "optimization_target_cfg,read_latency(ns),write_latency(ns),"
            "area(mm2),read_dynamic_energy(pJ),write_dynamic_energy(pJ),"
            "leakage_power(uW),status,return_code,log_file,exploration_csv\n";

        // Per-capacity header (same columns, capacity is redundant but kept
        // so files are self-contained and easy to merge later).
        static const std::string& PER_CAP_HEADER = FULL_HEADER;

        // 1) Write full combined CSV (all rows, all capacities)
        {
            std::ofstream csv(csv_path);
            if (!csv) {
                std::cerr << "[ERROR] Cannot write CSV: " << csv_path << "\n";
                return 2;
            }
            csv << FULL_HEADER;
            for (const auto& r : rows) write_row(csv, r);
        }

        // 2) Group rows by capacity key
        std::map<std::string, std::vector<const Row*>> by_capacity;
        for (const auto& r : rows) {
            by_capacity[capacity_key(r.capacity_value, r.capacity_unit)].push_back(&r);
        }

        // 3) Write per-capacity Pareto-filtered CSVs
        size_t total_pareto = 0;
        for (const auto& [cap_key, group] : by_capacity) {
            auto pareto_idx = pareto_front_indices(group);
            total_pareto += pareto_idx.size();

            fs::path per_cap_path = out_dir / ("pareto_" + cap_key + ".csv");
            std::ofstream pcsv(per_cap_path);
            if (!pcsv) {
                std::cerr << "[WARN] Cannot write per-capacity CSV: " << per_cap_path << "\n";
                continue;
            }
            pcsv << PER_CAP_HEADER;
            for (size_t idx : pareto_idx) write_row(pcsv, *group[idx]);

            std::cout << "  Pareto " << cap_key << ": " << pareto_idx.size()
                      << " / " << group.size() << " designs -> "
                      << fs::relative(per_cap_path, config_dir).string() << "\n";
        }

        std::cout << "\nDone. Wrote " << rows.size() << " total rows to: "
                  << csv_path.string() << "\n"
                  << "  Pareto-filtered per-capacity CSVs: " << total_pareto
                  << " designs across " << by_capacity.size() << " capacity bins\n"
                  << "Logs saved under: "
                  << (fs::current_path() / OUT_DIR / "logs").string() << "\n"
                  << "Temp cfgs saved under: "
                  << (fs::current_path() / OUT_DIR / "tmp_cfgs").string() << "\n";

        return 0;
   
    }
    catch (const std::exception& e) {
        std::cerr << "[FATAL] " << e.what() << "\n";
        return 2;
    }
}


