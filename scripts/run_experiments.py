#!/usr/bin/env python3
"""Run the Weiss benchmark or a timed TT layout round robin."""

import argparse
import csv
import hashlib
import math
import os
import platform
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ======================== EDIT EXPERIMENT SETTINGS HERE ========================
# Exact Weiss defaults used as the starting point for every settings list.
WEISS_DEFAULT = {
    "bucket_size": 2,
    "depth": 14,
    "threads": 1,
    "hash_mb": 32,
    "cap_helpers": 0,
    "rotate_root_moves": 0,
    "cpu_binding": "none",
}
# The Weiss default is always included. For each study, edit only EXTRA_RUNS;
# each named configuration inherits every default it does not explicitly change.
EXTRA_RUNS = [
    {"name": "threads-2", "threads": 2},
    {"name": "threads-4", "threads": 4},
    {"name": "threads-8", "threads": 8},
    {"name": "threads-16", "threads": 16},
    {"name": "threads-32", "threads": 32},
    {"name": "threads-64", "threads": 64},
    {"name": "threads-128", "threads": 128},
    {"name": "threads-192", "threads": 192},
]
REPETITIONS = 5
ORDER_SEED = 2026                   # Randomizes paired config order and pair order.
CC = "gcc"
TIMEOUT_SECONDS = 300               # Whole 50-position process; None disables.
EXPERIMENT_SLUG = "thread-scaling"
TIME_BUDGET_HOURS = 6.5             # Includes builds, affinity checks, bench and self-play.
CHECK_THREAD_AFFINITY = True        # Diagnostic only; checks run before timed benchmarks.
AFFINITY_CHECK_TIMEOUT_SECONDS = 30

# This is the reference engine code. Every candidate run is paired against it
# with identical benchmark arguments. Change only when intentionally resetting
# the research control; record the new reference in the thesis methods.
BASELINE_GIT_REF = "c55272499a460acdb7bd057ad55d495b65faaefd"

# Run a small UCI self-play smoke test after all benchmark pairs. It checks that
# both builds can play legal games; four games cannot establish playing strength.
SELFPLAY_ENABLED = True
SELFPLAY_REQUIRED = True             # Require the routine smoke check before any runs start.
FASTCHESS_CLI = "fastchess"
SELFPLAY_GAMES = 4                    # Must be even; colors are swapped in pairs.
SELFPLAY_TIME_CONTROL = "1+0.1"
SELFPLAY_HASH_MB = WEISS_DEFAULT["hash_mb"]
SELFPLAY_THREADS = WEISS_DEFAULT["threads"]
# Separate playing-strength experiment: pass --round-robin and an EPD book.
ROUND_ROBIN_BUCKET_SIZES = [1, 2, 4]
OPENING_PAIRS_PER_MATCHUP = 3
ROUND_ROBIN_THREADS = 1
ROUND_ROBIN_HASH_MB = 64
ROUND_ROBIN_TIME_CONTROL = "10+0.1"
ROUND_ROBIN_SLUG = "tt-layout-round-robin"

# Full-bucket victim comparison. Choose timing or diagnostics with WEISS_TT_VICTIM_TEST.
# Keep 64 MiB for the policy study. Add sizes here for a separate capacity sweep.
VICTIM_HASH_MIB = [64]
VICTIM_TEST = os.environ.get("WEISS_TT_VICTIM_TEST", "")
TT_DIAGNOSTICS = os.environ.get("WEISS_TT_DIAGNOSTICS", "0") == "1"
if VICTIM_TEST and VICTIM_TEST not in ("timing", "diagnostics"):
    raise ValueError("WEISS_TT_VICTIM_TEST must be timing or diagnostics")
if VICTIM_TEST == "timing" and TT_DIAGNOSTICS:
    raise ValueError("Unset WEISS_TT_DIAGNOSTICS for the uninstrumented victim timing test")
if VICTIM_TEST or TT_DIAGNOSTICS:
    WEISS_DEFAULT.update(bucket_size=2, depth=23, threads=1, hash_mb=64)
    REPETITIONS = 10
    TIME_BUDGET_HOURS = 8
    TIMEOUT_SECONDS = 600
    BASELINE_GIT_REF = None
    SELFPLAY_ENABLED = False
    if VICTIM_TEST:
        if not VICTIM_HASH_MIB:
            raise ValueError("VICTIM_HASH_MIB needs at least one TT size")
        TT_DIAGNOSTICS = VICTIM_TEST == "diagnostics"
        WEISS_DEFAULT.update(hash_mb=VICTIM_HASH_MIB[0], cap_helpers=0, rotate_root_moves=0, cpu_binding="none")
        EXTRA_RUNS = [{"name": f"hash-{size}-t{threads}", "hash_mb": size, "threads": threads}
                      for size in VICTIM_HASH_MIB for threads in (1, 128)
                      if (size, threads) != (VICTIM_HASH_MIB[0], 1)]
        EXPERIMENT_SLUG = f"tt-victim-policy-{VICTIM_TEST}-depth23"
    else:
        EXTRA_RUNS = [{"name": f"b{bucket}-t{threads}", "bucket_size": bucket, "threads": threads}
                      for bucket in (1, 2, 4) for threads in (1, 128)
                      if (bucket, threads) != (2, 1)]
        EXPERIMENT_SLUG = "tt-replacement-diagnostics-depth23"

# Set cpu_binding="physical" in an EXTRA_RUNS entry to pin candidate workers to
# one allowed logical CPU per physical core on Linux. Default placement is inherited.
# ==============================================================================

POSITION_FIELDS = ["position", "depth", "wall_ns", "cpu_ns", "main_nodes", "nodes",
                   "bestmove", "score_type", "score", "raw_score",
                   "helper_max_started_iteration", "helper_max_completed_iteration",
                   "helper_nodes_beyond_depth"]
PHASE_TIMING_FIELDS = ["helper_launch_ns", "time_to_depth_ns", "helper_stop_to_join_ns"]
CONFIG_FIELDS = ["bucket_size", "depth", "threads", "hash_mb", "cap_helpers",
                 "rotate_root_moves", "cpu_binding"]
RUN_FIELDS = ["run", "comparison_id", "condition", "config_name", "repetition",
              *CONFIG_FIELDS, "phase"]
PROVENANCE_FIELDS = ["binary_sha256", "raw_log"] + (["victim_policy"] if VICTIM_TEST else [])
OVERALL = re.compile(r"^OVERALL:\s+(\d+)\s+ms\s+(\d+)\s+nodes\s+(\d+)\s+nps\s*$")
AFFINITY_FIELDS = ["check", "bucket_size", "cap_helpers", "threads", "thread_index",
                   "expected_cpu", "running_cpu", "allowed_cpu_count", "allowed_cpus",
                   "status", "raw_log"]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_fastchess():
    """Return Fastchess's path if it is available on PATH."""
    return shutil.which(FASTCHESS_CLI)


def experiment_directory(results_root, slug, now=None):
    """Return a dated, descriptive output path without creating it."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Experiment slug must use lowercase letters, numbers and hyphens")
    date = (datetime.now(timezone.utc) if now is None else now).strftime("%Y%m%d")
    stem = f"{date}_{slug}"
    candidate = Path(results_root) / stem
    suffix = 2
    while candidate.exists():
        candidate = Path(results_root) / f"{stem}_{suffix:02d}"
        suffix += 1
    return candidate


def _markdown_value(value):
    if isinstance(value, dict):
        return "; ".join(f"{key}: {item}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return ", ".join(map(str, value))
    return str(value)


def write_experiment_md(output_dir, *, title, question, hypothesis, settings,
                        controls, source_commit, source_status,
                        binaries=(), status="planned", results="Pending.",
                        conclusion="Pending manual interpretation.", limitations=()):
    """Write the concise, reproducible record that accompanies every run."""
    lines = [f"# {title}", "", f"- **Run ID:** `{Path(output_dir).name}`",
             f"- **Status:** {status}", "", "## Question", "", question, "",
             "## Hypothesis", "", hypothesis, "", "## Settings", ""]
    lines.extend(f"- **{key}:** {_markdown_value(value)}" for key, value in settings.items())
    lines += ["", "## Controls", ""]
    lines.extend(f"- {item}" for item in controls)
    lines += ["", "## Source and build identifiers", "",
              f"- Source commit: `{source_commit}`",
              f"- Source status before run: {source_status or '(clean)' }",
              "- Runner snapshot: `runner.py`"]
    if binaries:
        lines += [f"- `{name}` SHA-256: `{digest}`" for name, digest in binaries]
    lines += ["", "## Results", "", results, "", "## Conclusion", "", conclusion,
              "", "## Limitations", ""]
    lines.extend(f"- {item}" for item in limitations)
    Path(output_dir, "experiment.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def benchmark_results_text(output_dir):
    """Summarize completed benchmark rows without making a significance claim."""
    path = Path(output_dir) / "runs.csv"
    if not path.exists():
        return "No completed benchmark runs have been recorded yet."
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        return "No complete benchmark runs were recorded."
    dimensions = ("bucket_size", "depth", "threads", "hash_mb", "cap_helpers",
                  "rotate_root_moves", "cpu_binding")
    groups = {}
    for row in rows:
        key = (row["condition"], row["config_name"]) + tuple(
            row.get(name, "none") if name == "cpu_binding" else int(row.get(name) or 0)
            for name in dimensions)
        group = groups.setdefault(key, {"wall": [], "nodes": [], "main_nodes": []})
        group["wall"].append(int(row["wall_ns"]) / 1e9)
        group["nodes"].append(int(row["nodes"]))
        group["main_nodes"].append(int(row["main_nodes"]))
    lines = [f"Completed benchmark runs: {len(rows)}.", "",
             "| Build | Config | Bucket | Depth | Workers | TT MiB | Cap | Rotation | Binding | Mean wall s | Mean nodes | Mean main nodes |",
             "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|"]
    for key, values in sorted(groups.items()):
        means = [sum(values[name]) / len(values[name])
                 for name in ("wall", "nodes", "main_nodes")]
        lines.append("| " + " | ".join(map(str, key)) + " | "
                     + f"{means[0]:.4f} | {means[1]:.0f} | {means[2]:.0f} |")
    return "\n".join(lines)


def write_comparisons(output_dir):
    """Write one paired row only when both equal-work runs completed."""
    run_path = Path(output_dir) / "runs.csv"
    if not run_path.exists():
        return
    with run_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    pairs = {}
    for row in rows:
        pairs.setdefault(row["comparison_id"], {})[row["condition"]] = row
    fields = ["comparison_id", "config_name", "repetition", "bucket_size", "depth", "threads", "hash_mb",
              "cap_helpers", "rotate_root_moves", "cpu_binding",
              "baseline_applied_cpu_binding", "candidate_applied_cpu_binding",
              "baseline_cpu_list", "candidate_cpu_list",
              "baseline_run", "candidate_run",
              "baseline_wall_ns", "candidate_wall_ns", "speedup_baseline_over_candidate",
              "baseline_cpu_ns", "candidate_cpu_ns", "baseline_nodes", "candidate_nodes",
              "baseline_main_nodes", "candidate_main_nodes",
              "baseline_binary_sha256", "candidate_binary_sha256"]
    comparison_path = Path(output_dir) / "comparisons.csv"
    with comparison_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for comparison_id, pair in sorted(pairs.items()):
            if set(pair) != {"baseline", "candidate"}:
                continue
            baseline, candidate = pair["baseline"], pair["candidate"]
            row = {key: baseline[key] for key in (
                "comparison_id", "config_name", "repetition", "bucket_size", "depth", "threads",
                "hash_mb", "cap_helpers", "rotate_root_moves", "cpu_binding")}
            for label, run in (("baseline", baseline), ("candidate", candidate)):
                row[f"{label}_run"] = run["run"]
                row[f"{label}_binary_sha256"] = run["binary_sha256"]
                row[f"{label}_applied_cpu_binding"] = run.get("applied_cpu_binding", "none")
                row[f"{label}_cpu_list"] = run.get("cpu_list", "none")
                for metric in ("wall_ns", "cpu_ns", "nodes", "main_nodes"):
                    row[f"{label}_{metric}"] = run[metric]
            row["speedup_baseline_over_candidate"] = (
                int(baseline["wall_ns"]) / int(candidate["wall_ns"]))
            writer.writerow(row)


def write_position_comparisons(output_dir):
    """Join move/evaluation and timing outcomes for matched positions."""
    path = Path(output_dir) / "positions.csv"
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    pairs = {}
    for row in rows:
        pair = pairs.setdefault(row["comparison_id"], {"baseline": {}, "candidate": {}})
        pair[row["condition"]][row["position"]] = row
    fields = ["comparison_id", "config_name", "repetition", "bucket_size", "depth", "threads", "hash_mb",
              "cap_helpers", "rotate_root_moves", "cpu_binding", "position", "baseline_wall_ns",
              "candidate_wall_ns", "baseline_speedup_over_candidate", "baseline_nodes",
              "candidate_nodes", "baseline_cpu_ns", "candidate_cpu_ns",
              "baseline_main_nodes", "candidate_main_nodes",
              "baseline_bestmove", "candidate_bestmove", "same_move", "baseline_score_type",
              "candidate_score_type", "baseline_score", "candidate_score", "score_delta_cp"]
    result_path = Path(output_dir) / "position-comparisons.csv"
    with result_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for comparison_id, pair in sorted(pairs.items()):
            baseline, candidate = pair["baseline"], pair["candidate"]
            if not baseline or set(baseline) != set(candidate):
                continue
            for position in sorted(baseline, key=int):
                left, right = baseline[position], candidate[position]
                row = {key: left[key] for key in (
                    "comparison_id", "config_name", "repetition", "bucket_size", "depth", "threads",
                    "hash_mb", "cap_helpers", "rotate_root_moves", "cpu_binding", "position")}
                for label, source in (("baseline", left), ("candidate", right)):
                    for metric in ("wall_ns", "cpu_ns", "nodes", "main_nodes",
                                   "bestmove", "score_type", "score"):
                        row[f"{label}_{metric}"] = source[metric]
                row["baseline_speedup_over_candidate"] = (
                    int(left["wall_ns"]) / int(right["wall_ns"]))
                row["same_move"] = int(left["bestmove"] == right["bestmove"])
                row["score_delta_cp"] = (
                    int(right["score"]) - int(left["score"])
                    if left["score_type"] == right["score_type"] == "cp" else "")
                writer.writerow(row)


def capture(command, required=False):
    """Read metadata; missing optional system tools are recorded, not installed."""
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        if required:
            raise RuntimeError(f"{shlex.join(command)}: {error}") from error
        return str(error)
    if required and result.returncode:
        raise RuntimeError(f"{shlex.join(command)}: {result.stderr.strip()}")
    return (result.stdout + result.stderr).strip()


def validate_configuration(config):
    if set(config) != set(CONFIG_FIELDS):
        raise ValueError(f"Configuration must define exactly: {', '.join(CONFIG_FIELDS)}")
    ranges = {"bucket_size": (1, 8), "depth": (1, 100), "threads": (1, 2048),
              "hash_mb": (2, None), "cap_helpers": (0, 1), "rotate_root_moves": (0, 1)}
    for name, (minimum, maximum) in ranges.items():
        value = config[name]
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"Invalid {name}: {value!r}")
    if config["bucket_size"] not in (1, 2, 4, 8):
        raise ValueError("bucket_size supports only 1, 2, 4, 8")
    if config["cpu_binding"] not in ("none", "physical"):
        raise ValueError("cpu_binding must be 'none' or 'physical'")
    if config["cpu_binding"] == "physical" and not sys.platform.startswith("linux"):
        raise ValueError("cpu_binding='physical' is supported only on Linux")
    if sys.platform.startswith("linux") and config["hash_mb"] % 2:
        raise ValueError("hash_mb must be even on Linux")


def physical_core_cpu_ids():
    """Return one allowed Linux logical CPU for each physical core."""
    if not sys.platform.startswith("linux") or not hasattr(os, "sched_getaffinity"):
        raise ValueError("physical CPU binding requires Linux sched_getaffinity support")
    allowed = sorted(os.sched_getaffinity(0))
    if not allowed:
        raise ValueError("The current process has no allowed CPUs")
    cores = {}
    for cpu in allowed:
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            package = int((topology / "physical_package_id").read_text().strip())
            core = int((topology / "core_id").read_text().strip())
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read Linux core topology for CPU {cpu}: {error}") from error
        if package < 0 or core < 0:
            raise ValueError(f"Linux reports invalid physical-core topology for CPU {cpu}")
        cores.setdefault((package, core), cpu)
    cpu_ids = [cores[key] for key in sorted(cores)]
    if any(cpu >= 1024 for cpu in cpu_ids):
        raise ValueError("CPU IDs >= 1024 are unsupported by the current affinity implementation")
    return cpu_ids


def resolve_cpu_binding(plan):
    """Preflight the requested physical-core bindings and return their CPU map."""
    requested = [run["threads"] for run in plan if run["cpu_binding"] == "physical"]
    if not requested:
        return []
    cpu_ids = physical_core_cpu_ids()
    needed = max(requested)
    if needed > len(cpu_ids):
        raise ValueError(f"Physical binding needs {needed} cores, but only {len(cpu_ids)} are allowed")
    return cpu_ids


def worker_environment(run, physical_cpu_ids):
    """Set the private candidate-engine affinity map for this process only."""
    environment = os.environ.copy()
    environment.pop("WEISS_CPU_LIST", None)
    if run.get("cpu_binding") == "physical":
        environment["WEISS_CPU_LIST"] = ",".join(
            str(cpu) for cpu in physical_cpu_ids[:run["threads"]])
    return environment


def affinity_check_specs(plan):
    """Deduplicate candidate affinity checks by build variant and worker count."""
    return sorted({(run["bucket_size"], run["cap_helpers"], run["threads"])
                   for run in plan
                   if run["condition"] == "candidate" and run["cpu_binding"] == "physical"})


def parse_affinity_check(path, *, check_id, bucket_size, cap_helpers, threads,
                         physical_cpu_ids):
    """Return one observed mask per worker and whether all masks match the requested CPUs."""
    observed = {}
    final_threads = None
    final_status = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = dict(re.findall(r"(\w+)=([^\s]+)", line))
        if line.startswith("AFFINITY_THREAD "):
            try:
                index = int(fields["thread"])
                observed[index] = fields
            except (KeyError, ValueError):
                continue
        elif line.startswith("AFFINITY_RESULT "):
            try:
                final_threads = int(fields["threads"])
                final_status = fields["status"]
            except (KeyError, ValueError):
                final_status = "fail"

    rows = []
    all_passed = final_threads == threads and final_status == "pass"
    for index in range(threads):
        fields = observed.get(index, {})
        expected_cpu = physical_cpu_ids[index]
        try:
            logged_expected = int(fields.get("expected_cpu", "-1"))
            running_cpu = int(fields.get("running_cpu", "-1"))
            allowed_count = int(fields.get("allowed_count", "0"))
        except ValueError:
            logged_expected, running_cpu, allowed_count = -1, -1, 0
        allowed_cpus = fields.get("allowed_cpus", "missing")
        status = fields.get("status", "missing")
        valid = (status == "pass" and logged_expected == expected_cpu
                 and running_cpu == expected_cpu
                 and allowed_count == 1 and allowed_cpus == str(expected_cpu))
        if not valid:
            all_passed = False
        rows.append({"check": check_id, "bucket_size": bucket_size,
                     "cap_helpers": cap_helpers, "threads": threads,
                     "thread_index": index, "expected_cpu": expected_cpu,
                     "running_cpu": running_cpu,
                     "allowed_cpu_count": allowed_count, "allowed_cpus": allowed_cpus,
                     "status": "pass" if valid else "fail",
                     "raw_log": str(path)})
    return rows, all_passed


def append_affinity_results(path, rows):
    if not rows:
        return
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=AFFINITY_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def make_plan(*, extra_runs=None, repetitions=None):
    """Create default plus explicit override runs; no Cartesian products."""
    extras = EXTRA_RUNS if extra_runs is None else extra_runs
    repetitions = REPETITIONS if repetitions is None else repetitions
    if not isinstance(extras, (list, tuple)):
        raise ValueError("EXTRA_RUNS must be a list of named settings overrides")
    if type(repetitions) is not int or repetitions < 1 or type(ORDER_SEED) is not int:
        raise ValueError("REPETITIONS must be positive and ORDER_SEED must be an integer")
    if not isinstance(CC, str) or not shlex.split(CC):
        raise ValueError("CC must name a compiler")
    if TIMEOUT_SECONDS is not None and (type(TIMEOUT_SECONDS) not in (int, float)
                                       or not math.isfinite(TIMEOUT_SECONDS) or TIMEOUT_SECONDS <= 0):
        raise ValueError("TIMEOUT_SECONDS must be positive or None")

    default_config = dict(WEISS_DEFAULT)
    validate_configuration(default_config)
    configurations = [("weiss-default", "default", default_config)]
    names = {"weiss-default"}
    signatures = {tuple(default_config[field] for field in CONFIG_FIELDS)}
    for extra in extras:
        if not isinstance(extra, dict) or "name" not in extra:
            raise ValueError("Each EXTRA_RUNS entry needs a name and one or more setting overrides")
        name = extra["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError("Extra-run names must use lowercase letters, numbers and hyphens")
        if name in names:
            raise ValueError(f"Duplicate experiment configuration name: {name}")
        names.add(name)
        overrides = {key: value for key, value in extra.items() if key != "name"}
        if not overrides or not set(overrides) <= set(CONFIG_FIELDS):
            raise ValueError(f"{name} must override at least one known Weiss setting")
        config = {**default_config, **overrides}
        validate_configuration(config)
        signature = tuple(config[field] for field in CONFIG_FIELDS)
        if signature in signatures:
            raise ValueError(f"{name} duplicates another configuration; default is already automatic")
        signatures.add(signature)
        configurations.append((name, "extra", config))

    rng = random.Random(ORDER_SEED)
    plan = []
    for repetition in range(1, repetitions + 1):
        block = configurations.copy()
        rng.shuffle(block)
        for name, phase, config in block:
            plan.append({"run": len(plan) + 1, "config_name": name,
                         "repetition": repetition, **config, "phase": phase})
    return plan


def make_experiment_plan(*, extra_runs=None, repetitions=None):
    """Pair every candidate configuration with an equal-work reference run."""
    candidates = make_plan(extra_runs=extra_runs, repetitions=repetitions)
    rng = random.Random(ORDER_SEED ^ 0x5E1F)
    plan = []
    for candidate in candidates:
        comparison_id = f"{candidate['config_name']}-rep{candidate['repetition']:02d}"
        pair = []
        for condition in ("baseline", "candidate"):
            pair.append({**candidate, "comparison_id": comparison_id,
                         "condition": condition})
        if rng.randrange(2):
            pair.reverse()
        plan.extend(pair)
    for number, run in enumerate(plan, 1):
        run["run"] = number
    return plan


def validate_paired_plan(plan):
    """Reject plans that do not give every source build the same requested work."""
    groups = {}
    parameters = ("config_name", "repetition", *CONFIG_FIELDS, "phase")
    for run in plan:
        if run.get("condition") not in ("baseline", "candidate") or not run.get("comparison_id"):
            raise ValueError("Every benchmark run needs a comparison_id and baseline/candidate condition")
        pair = groups.setdefault(run["comparison_id"], {})
        if run["condition"] in pair:
            raise ValueError(f"Duplicate {run['condition']} run in comparison {run['comparison_id']}")
        pair[run["condition"]] = run
    for comparison_id, pair in groups.items():
        if set(pair) != {"baseline", "candidate"}:
            raise ValueError(f"Comparison {comparison_id} is missing its baseline or candidate run")
        baseline, candidate = pair["baseline"], pair["candidate"]
        if any(baseline.get(field) != candidate.get(field) for field in parameters):
            raise ValueError(f"Baseline and candidate settings differ in comparison {comparison_id}")
    return groups


TT_COUNTERS = ("hit empty_miss full_miss ab_probes qs_probes ab_cutoffs qs_cutoffs "
               "first_tt_move first_tt_move_cutoff store_empty store_same store_replace store_skip "
               "replaced_depth_sum replaced_depth_0 replaced_depth_1_7 replaced_depth_8_15 "
               "replaced_depth_16_plus sampled_stores changed_slots changed_deeper_replacements").split()


def tt_fields(bucket_size):
    return ([f"tt_buckets_{n}" for n in range(bucket_size + 1)]
            + [f"tt_{role}_{metric}" for role in ("main", "helpers") for metric in TT_COUNTERS])


def validate_tt_counts(row, config):
    width = config["bucket_size"]
    if any(row[field] < 0 for field in tt_fields(width)):
        raise ValueError("Negative TT counter")
    if sum(row[f"tt_buckets_{n}"] for n in range(width + 1)) != config["buckets"]:
        raise ValueError("TT occupancy histogram does not cover every bucket")
    depth_bins = ("replaced_depth_0", "replaced_depth_1_7", "replaced_depth_8_15", "replaced_depth_16_plus")
    for role in ("main", "helpers"):
        c = {m: row[f"tt_{role}_{m}"] for m in TT_COUNTERS}
        if (c["hit"] + c["empty_miss"] + c["full_miss"] != c["ab_probes"] + c["qs_probes"]
                or c["ab_cutoffs"] > c["ab_probes"] or c["qs_cutoffs"] > c["qs_probes"]
                or c["ab_cutoffs"] + c["qs_cutoffs"] > c["hit"]
                or c["first_tt_move_cutoff"] > c["first_tt_move"]
                or sum(c[m] for m in depth_bins) != c["store_replace"]
                or not c["changed_deeper_replacements"] <= c["changed_slots"] <= c["sampled_stores"]
                or (role == "helpers" and config["threads"] == 1 and any(c.values()))):
            raise ValueError(f"Inconsistent TT diagnostics at position {row['position']} ({role})")


def victim_policy(condition):
    return "depth-blind" if VICTIM_TEST and condition == "candidate" else "depth-age"


def use_tt_counters(condition):
    return TT_DIAGNOSTICS and (bool(VICTIM_TEST) or condition == "candidate")


def parse_result(path, expected, physical_cpu_ids=None):
    """Accept only a complete, internally consistent log for the requested run."""
    configs, headers, positions, totals, overall = [], [], [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("BENCH_CONFIG "):
            configs.append(dict(re.findall(r"(\w+)=([^\s]+)", line)))
        elif line.startswith("BENCH_POSITION_HEADER,"):
            headers.append(next(csv.reader([line]))[1:])
        elif line.startswith("BENCH_POSITION,"):
            values = next(csv.reader([line]))[1:]
            if not headers or len(values) != len(headers[-1]):
                raise ValueError(f"Malformed position row in {path}")
            row = dict(zip(headers[-1], values))
            for field in row:
                if field not in ("bestmove", "score_type"):
                    row[field] = int(row[field])
            positions.append(row)
        elif line.startswith("BENCH_TOTAL "):
            totals.append(dict(re.findall(r"(\w+)=(\d+)", line)))
        elif match := OVERALL.fullmatch(line):
            overall.append(tuple(map(int, match.groups())))
    timed_fields = POSITION_FIELDS + PHASE_TIMING_FIELDS
    detailed_fields = timed_fields + tt_fields(expected["bucket_size"])
    detailed = headers == [detailed_fields]
    if (VICTIM_TEST or TT_DIAGNOSTICS) and detailed != use_tt_counters(expected.get("condition")):
        raise ValueError(f"TT diagnostics do not match requested build in {path}")
    if (len(configs) != 1 or headers not in ([POSITION_FIELDS], [timed_fields], [detailed_fields])
            or len(totals) != 1 or len(overall) != 1):
        raise ValueError(f"Missing or duplicate benchmark records in {path}")
    string_fields = {"cpu_binding", "cpu_list", "victim_policy"}
    config = {key: (value if key in string_fields else int(value))
              for key, value in configs[0].items()}
    if VICTIM_TEST:
        policy = victim_policy(expected["condition"])
        if config.get("victim_policy") != policy:
            raise ValueError(f"Expected engine victim policy {policy} in {path}")
    config.setdefault("rotate_root_moves", 0)  # Older benchmark logs used the unrotated order.
    config.setdefault("cpu_binding", "none")
    config.setdefault("cpu_list", "none")
    if not {"bucket_size", "depth", "threads", "hash_mb", "entry_bytes", "bucket_bytes", "buckets", "cap_helpers", "rotate_root_moves"} <= config.keys():
        raise ValueError(f"Missing benchmark configuration fields in {path}")
    if any(config.get(key) != expected.get(key, 0) for key in ("bucket_size", "depth", "threads", "hash_mb", "cap_helpers", "rotate_root_moves")):
        raise ValueError(f"Benchmark configuration differs from requested run in {path}")
    requested_binding = expected.get("cpu_binding", "none")
    applied_binding = config["cpu_binding"]
    expected_modes = ({"none"} if requested_binding == "none" else
                      {"physical"} if expected.get("condition") == "candidate" else
                      {"none", "physical"})
    if applied_binding not in expected_modes:
        raise ValueError(f"Engine applied CPU binding {applied_binding!r} unexpectedly in {path}")
    expected_cpu_list = "none"
    if applied_binding == "physical":
        if physical_cpu_ids is None or len(physical_cpu_ids) < expected["threads"]:
            raise ValueError("Missing physical CPU map for a bound benchmark run")
        expected_cpu_list = ",".join(str(cpu) for cpu in physical_cpu_ids[:expected["threads"]])
    if config["cpu_list"] != expected_cpu_list:
        raise ValueError(f"Engine CPU list differs from the requested binding in {path}")
    if config["bucket_bytes"] != config["bucket_size"] * config["entry_bytes"] or (
            config["buckets"] * config["bucket_bytes"] != config["hash_mb"] * 1024 * 1024):
        raise ValueError(f"Inconsistent TT layout in {path}")
    if [row["position"] for row in positions] != list(range(1, 51)):
        raise ValueError(f"Expected exactly positions 1..50 in {path}")
    for row in positions:
        if (row["depth"] != expected["depth"] or row["wall_ns"] <= 0 or row["cpu_ns"] < 0
                or not 0 <= row["main_nodes"] <= row["nodes"]
                or (expected["threads"] == 1 and row["main_nodes"] != row["nodes"])
                or not re.fullmatch(r"[a-h][1-8][a-h][1-8][nbrq]?", row["bestmove"])
                or row["score_type"] not in ("cp", "mate")
                or (row["score_type"] == "cp" and row["score"] != row["raw_score"])):
            raise ValueError(f"Invalid or incomplete position {row['position']} in {path}")
        started = row["helper_max_started_iteration"]
        completed = row["helper_max_completed_iteration"]
        beyond = row["helper_nodes_beyond_depth"]
        if (not 0 <= completed <= started <= 100
                or not 0 <= beyond <= row["nodes"] - row["main_nodes"]
                or (beyond > 0 and started <= expected["depth"])
                or (expected["cap_helpers"] and (started > expected["depth"] or beyond != 0))
                or (expected["threads"] == 1 and (started or completed or beyond))):
            raise ValueError(f"Invalid helper telemetry in {path}")
    summary = {key: sum(row[key] for row in positions)
               for key in ("wall_ns", "cpu_ns", "main_nodes", "nodes")}
    if any(int(totals[0].get(key, -1)) != summary[key] for key in ("wall_ns", "cpu_ns")):
        raise ValueError(f"Clock totals do not match position rows in {path}")
    elapsed_ms, nodes, nps = overall[0]
    if nodes != summary["nodes"] or elapsed_ms != max(1, summary["wall_ns"] // 1_000_000):
        raise ValueError(f"OVERALL does not match position rows in {path}")
    if detailed:
        for row in positions:
            validate_tt_counts(row, config)
    if detailed or headers == [timed_fields]:
        for row in positions:
            launch = row["helper_launch_ns"]
            to_depth = row["time_to_depth_ns"]
            stop_to_join = row["helper_stop_to_join_ns"]
            if (not 0 <= launch <= to_depth <= row["wall_ns"] or to_depth == 0
                    or not 0 <= stop_to_join <= row["wall_ns"] - to_depth
                    or (expected["threads"] == 1 and (launch or stop_to_join))):
                raise ValueError(f"Invalid phase timing for position {row['position']} in {path}")
        summary.update({field: sum(row[field] for row in positions)
                        for field in PHASE_TIMING_FIELDS})
    summary.update(
        helper_nodes_beyond_depth=sum(row["helper_nodes_beyond_depth"] for row in positions),
        helper_max_started_iteration=max(row["helper_max_started_iteration"] for row in positions),
        helper_max_completed_iteration=max(row["helper_max_completed_iteration"] for row in positions))
    summary.update(elapsed_ms=elapsed_ms, nps=nps,
                   **{key: config[key] for key in ("entry_bytes", "bucket_bytes", "buckets")})
    summary.update(applied_cpu_binding=applied_binding, cpu_list=config["cpu_list"])
    return summary, positions


def build_command(bucket, binary, cap_helpers=0, source_root=ROOT, condition=None):
    flags = f"-DBENCH_CAP_HELPERS={cap_helpers}"
    if victim_policy(condition) == "depth-blind":
        flags += " -DTT_DEPTH_BLIND"
    if condition and use_tt_counters(condition):
        flags += " -DTT_DIAGNOSTICS"
    return ["make", "-C", str(Path(source_root) / "src"), "basic", f"BUCKET_SIZE={bucket}",
            f"CPPFLAGS={flags}", f"CC={CC}", f"EXE={binary}"]


def prepare_baseline_source(output_dir):
    """Check out the pinned control source without changing the user's checkout."""
    if not BASELINE_GIT_REF:
        return ROOT, None
    worktree = Path(output_dir) / "baseline-source"
    command = ["git", "worktree", "add", "--detach", str(worktree), BASELINE_GIT_REF]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Could not prepare baseline source: " + result.stderr.strip())
    return worktree, worktree


def remove_baseline_source(worktree):
    if worktree is None:
        return
    result = subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                            cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Could not remove temporary baseline worktree: " + result.stderr.strip())


def bench_command(run, binary):
    command = [str(binary), "bench", str(run["depth"]), str(run["threads"]), str(run["hash_mb"])]
    # Zero is the historical default; omit it to keep older command expectations valid.
    if run.get("rotate_root_moves", 0):
        command.append(str(run["rotate_root_moves"]))
    return command


def record_metadata(output_dir, deadline=None, physical_cpu_ids=None, *, include_fastchess=False):
    commit = capture(["git", "rev-parse", "HEAD"], required=True)
    status = capture(["git", "status", "--short"], required=True)
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    (output_dir / "source.patch").write_bytes(patch)
    shutil.copyfile(Path(__file__).resolve(), output_dir / "runner.py")
    details = [f"started_utc: {utc_now()}", f"hostname: {platform.node()}",
               f"platform: {platform.platform()}", f"machine: {platform.machine()}",
               f"python: {sys.version}", f"source_commit: {commit}",
               f"source_status_before_run:\n{status or '(clean)'}",
               f"runner_command: {shlex.join(sys.argv)}", f"order_seed: {ORDER_SEED}",
               f"baseline_git_ref: {BASELINE_GIT_REF or '(current checkout)'}",
               f"victim_test: {VICTIM_TEST or 'disabled'}",
               f"tt_diagnostics_baseline: {use_tt_counters('baseline')}",
               f"tt_diagnostics_candidate: {TT_DIAGNOSTICS}",
               f"check_thread_affinity: {CHECK_THREAD_AFFINITY}",
               f"timeout_seconds: {TIMEOUT_SECONDS}",
               f"total_budget_seconds_at_metadata: {None if deadline is None else max(0, deadline - time.monotonic())}",
               f"allowed_cpus: {sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else 'unavailable'}",
               f"physical_core_cpu_map: {physical_cpu_ids or 'not requested'}",
               ("placement: candidate workers use one CPU per physical core; reference source remains unpinned"
                if physical_cpu_ids else "placement: inherited from shell or scheduler; no per-worker binding requested")]
    fastchess = find_fastchess() if SELFPLAY_ENABLED or include_fastchess else None
    if fastchess:
        details.append(f"\n$ {shlex.join([fastchess, '-version'])}\n"
                       f"{capture([fastchess, '-version'])}")
    for command in ([*shlex.split(CC), "--version"], ["make", "--version"], ["lscpu"],
                    ["numactl", "--show"], ["numactl", "--hardware"]):
        details.append(f"\n$ {shlex.join(command)}\n{capture(command)}")
    for name in ("/proc/self/status", "/proc/meminfo", "/sys/fs/cgroup/cpu.max",
                 "/sys/kernel/mm/transparent_hugepage/enabled",
                 "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"):
        try:
            details.append(f"\n{name}\n{Path(name).read_text()}")
        except OSError:
            pass
    (output_dir / "run-info.txt").write_text("\n".join(details) + "\n", encoding="utf-8")
    return {"commit": commit, "status": status}


class BudgetExpired(RuntimeError):
    pass


def budget_timeout(deadline, per_run_limit=None):
    """Bound subprocess wall time; the clock includes clearing and process startup."""
    if deadline is None:
        return per_run_limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BudgetExpired("Experiment wall-time budget exhausted")
    return min(remaining, per_run_limit) if per_run_limit is not None else remaining


def run_command(command, file, timeout, env=None, check=True):
    """On timeout/interrupt, also stop compiler children rather than leaving jobs behind."""
    process = subprocess.Popen(command, cwd=ROOT, stdout=file, stderr=subprocess.STDOUT,
                               start_new_session=os.name == "posix", env=env)
    try:
        code = process.wait(timeout=timeout)
    except BaseException:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()
        raise
    if code and check:
        raise subprocess.CalledProcessError(code, command)
    return code


def pgn_result_headers(path):
    """Read only PGN headers, enough to summarize the smoke match."""
    games, tags = [], {}
    header = re.compile(r'^\[([A-Za-z0-9_]+)\s+"((?:[^"\\]|\\.)*)"\]\s*$')
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = header.fullmatch(line.strip())
        if match:
            # PGN permits tags after Result (Fastchess writes engine and timing
            # tags there), so a Result tag does not mean the header block ended.
            tags[match.group(1)] = match.group(2)
        elif not line.strip() and tags:
            # The blank line after the complete tag block separates headers
            # from moves. Collect the whole block, regardless of tag order.
            games.append(tags)
            tags = {}
    if tags:
        games.append(tags)
    required = {"White", "Black", "Result"}
    return [game for game in games if required.issubset(game)]


def run_affinity_checks(plan, output_dir, binaries, physical_cpu_ids, deadline):
    """Verify real worker affinity before the measured benchmark processes start."""
    if not CHECK_THREAD_AFFINITY:
        return 0
    specs = affinity_check_specs(plan)
    if not specs:
        return 0

    results_path = Path(output_dir) / "affinity-checks.csv"
    with results_path.open("w", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=AFFINITY_FIELDS).writeheader()

    checked_workers = 0
    for bucket, cap, threads in specs:
        binary = binaries["candidate", bucket, cap][0]
        check_id = f"b{bucket}-cap{cap}-t{threads}"
        raw = Path(output_dir) / "raw" / f"affinity-check-{check_id}.log"
        command = [str(binary), "affinity-check", str(threads)]
        print(f"Checking worker affinity: {check_id}", flush=True)
        with raw.open("w", encoding="utf-8") as log:
            code = run_command(command, log,
                               budget_timeout(deadline, AFFINITY_CHECK_TIMEOUT_SECONDS),
                               env=worker_environment({"cpu_binding": "physical",
                                                       "threads": threads}, physical_cpu_ids),
                               check=False)
        rows, passed = parse_affinity_check(
            raw, check_id=check_id, bucket_size=bucket, cap_helpers=cap,
            threads=threads, physical_cpu_ids=physical_cpu_ids)
        for row in rows:
            row["raw_log"] = str(raw.relative_to(output_dir))
        append_affinity_results(results_path, rows)
        checked_workers += len(rows)
        if code or not passed:
            raise RuntimeError(f"Thread affinity verification failed for {check_id}; see {raw}")
        print(f"  verified {threads} workers on distinct physical cores", flush=True)
    return checked_workers


def run_selfplay_smoke(output_dir, baseline_binary, candidate_binary, deadline):
    """Run a small color-swapped UCI smoke match with Fastchess."""
    status_path = Path(output_dir) / "selfplay-status.txt"
    fastchess = find_fastchess()
    if not SELFPLAY_ENABLED:
        status_path.write_text("disabled by settings\n", encoding="utf-8")
        return "disabled"
    if fastchess is None:
        message = f"skipped: cannot find {FASTCHESS_CLI!r}"
        status_path.write_text(message + "\n", encoding="utf-8")
        if SELFPLAY_REQUIRED:
            raise RuntimeError(message)
        print(f"Self-play smoke skipped: {message}", flush=True)
        return message
    if SELFPLAY_GAMES < 2 or SELFPLAY_GAMES % 2:
        raise ValueError("SELFPLAY_GAMES must be an even integer of at least 2")
    pgn_path = Path(output_dir) / "selfplay.pgn"
    command = [fastchess,
               "-engine", "name=Weiss candidate", "proto=uci", f"cmd={candidate_binary}",
               "-engine", "name=Weiss baseline", "proto=uci", f"cmd={baseline_binary}",
               "-each", "proto=uci", f"tc={SELFPLAY_TIME_CONTROL}",
               f"option.Threads={SELFPLAY_THREADS}", f"option.Hash={SELFPLAY_HASH_MB}",
               "-concurrency", "1", "-rounds", str(SELFPLAY_GAMES // 2), "-repeat",
               "-pgnout", f"file={pgn_path}", "append=false"]
    (Path(output_dir) / "selfplay-command.txt").write_text(
        shlex.join(command) + "\n", encoding="utf-8")
    status_path.write_text("running\n", encoding="utf-8")
    with (Path(output_dir) / "selfplay.log").open("w", encoding="utf-8") as log:
        run_command(command, log, budget_timeout(deadline, 1800),
                    env=worker_environment({"cpu_binding": "none"}, []))
    games = pgn_result_headers(pgn_path) if pgn_path.exists() else []
    candidate_name, baseline_name = "Weiss candidate", "Weiss baseline"
    wins = draws = losses = 0
    candidate_white = candidate_black = 0
    rows = []
    for number, game in enumerate(games, 1):
        white, black, result = game.get("White", ""), game.get("Black", ""), game.get("Result", "*")
        score = ""
        if {white, black} == {candidate_name, baseline_name}:
            candidate_white += int(white == candidate_name)
            candidate_black += int(black == candidate_name)
            if result == "1/2-1/2":
                draws += 1
                score = 0.5
            elif result in ("1-0", "0-1"):
                candidate_won = (result == "1-0" and white == candidate_name) or (
                    result == "0-1" and black == candidate_name)
                wins += int(candidate_won)
                losses += int(not candidate_won)
                score = int(candidate_won)
        rows.append({"game": number, "white": white, "black": black,
                     "result": result, "candidate_score": score})
    with (Path(output_dir) / "selfplay-games.csv").open("w", newline="", encoding="utf-8") as file:
        fields = ["game", "white", "black", "result", "candidate_score"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    completed = wins + draws + losses
    points = wins + 0.5 * draws
    score_pct = (100 * points / completed) if completed else ""
    summary = {"games_in_pgn": len(games), "completed_games": completed,
               "candidate_wins": wins, "draws": draws, "candidate_losses": losses,
               "candidate_score_pct": score_pct}
    with (Path(output_dir) / "selfplay-summary.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary))
        writer.writeheader()
        writer.writerow(summary)
    if (len(games) != SELFPLAY_GAMES or completed != SELFPLAY_GAMES
            or candidate_white != SELFPLAY_GAMES // 2
            or candidate_black != SELFPLAY_GAMES // 2):
        raise RuntimeError(
            f"Self-play smoke produced {completed}/{SELFPLAY_GAMES} complete games "
            f"with candidate colors {candidate_white} white/{candidate_black} black")
    status_path.write_text(f"complete: {completed} games; smoke test only\n", encoding="utf-8")
    print(f"Self-play smoke complete: {wins}-{draws}-{losses}; not an Elo estimate.", flush=True)
    return f"completed {completed} games; smoke test only"


def execute(plan, output_dir, deadline=None, physical_cpu_ids=None):
    if physical_cpu_ids is None:
        physical_cpu_ids = resolve_cpu_binding(plan)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "raw").mkdir()
    (output_dir / "bin").mkdir()
    status = output_dir / "status.txt"
    status.write_text("building\n")
    print(f"Results: {output_dir}", flush=True)
    source = {"commit": "not recorded", "status": "not recorded"}
    built_binaries = []

    def update_record(state, results=None, conclusion=None):
        settings = {
            "Configurations (default is always included)": sorted({run["config_name"] for run in plan}),
            "Weiss default settings": WEISS_DEFAULT,
            "Paired benchmark comparisons": len(plan) // 2,
            "Planned benchmark processes": len(plan),
            "Bucket sizes": sorted({run["bucket_size"] for run in plan}),
            "Depths": sorted({run["depth"] for run in plan}),
            "Total workers": sorted({run["threads"] for run in plan}),
            "TT sizes (MiB)": sorted({run["hash_mb"] for run in plan}),
            "Helper caps": sorted({run["cap_helpers"] for run in plan}),
            "Root move rotation": sorted({run["rotate_root_moves"] for run in plan}),
            "CPU binding modes": sorted({run["cpu_binding"] for run in plan}),
            "Physical core CPU map": physical_cpu_ids or "not requested",
            "Check thread affinity before timing": CHECK_THREAD_AFFINITY,
            "Repetitions": sorted({run["repetition"] for run in plan}),
            "Timeout": TIMEOUT_SECONDS,
            "Time budget (hours)": TIME_BUDGET_HOURS,
            "Baseline source": BASELINE_GIT_REF or "current checkout",
            "TT diagnostics": ("both builds" if use_tt_counters("baseline") else
                               "candidate only" if TT_DIAGNOSTICS else "disabled"),
            "Self-play smoke": (f"{SELFPLAY_GAMES} games at {SELFPLAY_TIME_CONTROL}; "
                                f"Hash {SELFPLAY_HASH_MB} MiB, Threads {SELFPLAY_THREADS}"
                                if SELFPLAY_ENABLED else "disabled"),
            "Self-play CLI": find_fastchess() if SELFPLAY_ENABLED else "disabled",
        }
        if VICTIM_TEST:
            settings["Full-bucket victim policy"] = "baseline depth-age, candidate depth-blind"
            question = "Does choosing a victim by depth and age improve a two-entry TT?"
            hypothesis = ("Depth-blind replacement may evict more deep entries, reduce immediate "
                          "cutoffs and increase time to depth at 128 workers.")
        elif TT_DIAGNOSTICS:
            question = "How does bucket width affect TT replacements and useful hits as worker count increases?"
            hypothesis = "Direct-mapped buckets may lose more reusable entries under parallel search."
        else:
            question = "How do changes in the current Weiss checkout affect time to depth, search work, moves and scores against the pinned source baseline?"
            hypothesis = "The candidate may change runtime and search results; the direction and size of the effect are measured with matched baseline/candidate runs."
        write_experiment_md(
            output_dir,
            title="Weiss paired benchmark experiment",
            question=question, hypothesis=hypothesis,
            settings=settings,
            controls=[
                "Each pair uses the same depth, workers, TT capacity, bucket width and ordered 50 positions.",
                "Each pair uses the baseline source specified above and the current checkout.",
                f"Pair order is randomized with seed {ORDER_SEED}; searches run sequentially.",
                *(["A short color-swapped self-play smoke check follows the benchmark."] if SELFPLAY_ENABLED else []),
                *(["Physical binding pins candidate workers only. Core selection uses the Linux cpuset and topology.",
                   "Worker affinity is checked separately before timing when enabled."]
                  if physical_cpu_ids else ["Both builds inherit CPU placement."]),
            ],
            source_commit=source["commit"], source_status=source["status"],
            binaries=built_binaries, status=state,
            results=results or benchmark_results_text(output_dir),
            conclusion=conclusion or "Compare completed paired rows in comparisons.csv.",
            limitations=[
                "Parallel searches may visit different numbers of nodes and may not be deterministic.",
                "Recorded means summarize completed runs only; incomplete configurations are not comparable as full repetitions.",
                "This benchmark measures time to a requested depth, not playing strength.",
            ],
        )

    try:
        source.update(record_metadata(output_dir, deadline, physical_cpu_ids) or {})
        update_record("building")
        with (output_dir / "plan.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_FIELDS)
            writer.writeheader()
            writer.writerows(plan)
        binaries = {}
        build_specs = {(run["condition"], run["bucket_size"], run["cap_helpers"])
                       for run in plan}
        if SELFPLAY_ENABLED:
            default_bucket = WEISS_DEFAULT["bucket_size"]
            default_cap = WEISS_DEFAULT["cap_helpers"]
            build_specs.update({("baseline", default_bucket, default_cap),
                                ("candidate", default_bucket, default_cap)})
        baseline_root, worktree = prepare_baseline_source(output_dir)
        source_roots = {"baseline": baseline_root, "candidate": ROOT}
        try:
            with (output_dir / "build-commands.txt").open("w") as commands:
                for condition, bucket, cap in sorted(build_specs):
                    binary = output_dir / "bin" / condition / f"weiss-b{bucket}-cap{cap}"
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    command = build_command(bucket, binary, cap, source_roots[condition],
                                            condition=condition)
                    commands.write(shlex.join(command) + "\n")
                    commands.flush()
                    print(f"Building {condition}: bucket size {bucket}, cap_helpers={cap}", flush=True)
                    log = output_dir / f"build-{condition}-b{bucket}-cap{cap}.log"
                    with log.open("w") as file:
                        run_command(command, file, budget_timeout(deadline))
                    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
                    binaries[condition, bucket, cap] = (binary, digest)
                    built_binaries.append((f"{condition}/{binary.name}", digest))
        finally:
            remove_baseline_source(worktree)
        update_record("checking-affinity", results="Builds completed; checking Linux worker CPU masks before timing.")
        checked_workers = run_affinity_checks(
            plan, output_dir, binaries, physical_cpu_ids, deadline)
        if checked_workers:
            affinity_summary = (f"Affinity check passed for {checked_workers} workers; "
                                "benchmark runs are in progress.")
        else:
            affinity_summary = ("No physical CPU binding requested, so the affinity preflight "
                                "was skipped; benchmark runs are in progress.")
        update_record("running", results=affinity_summary)
        summary_fields = ["wall_ns", "cpu_ns", "main_nodes", "nodes", "elapsed_ms", "nps",
                          "entry_bytes", "bucket_bytes", "buckets", "process_wall_ns",
                          "helper_nodes_beyond_depth", "helper_max_started_iteration",
                          "helper_max_completed_iteration", "applied_cpu_binding", "cpu_list",
                          *PHASE_TIMING_FIELDS]
        with (output_dir / "runs.csv").open("w", newline="") as run_file, (
                output_dir / "positions.csv").open("w", newline="") as position_file:
            runs = csv.DictWriter(run_file, fieldnames=RUN_FIELDS + ["started_utc", "finished_utc"]
                                  + summary_fields + PROVENANCE_FIELDS + ["command"])
            # Completed depth equals requested depth after validation; store it once.
            position_fields = RUN_FIELDS + [name for name in POSITION_FIELDS if name not in RUN_FIELDS]
            position_fields += PHASE_TIMING_FIELDS
            if TT_DIAGNOSTICS:
                position_fields += tt_fields(max(run["bucket_size"] for run in plan))
            positions = csv.DictWriter(position_file, fieldnames=position_fields + PROVENANCE_FIELDS)
            runs.writeheader()
            positions.writeheader()
            run_file.flush()
            position_file.flush()
            single_worker_results = {}
            for run in plan:
                binary, digest = binaries[run["condition"], run["bucket_size"], run["cap_helpers"]]
                command = bench_command(run, binary)
                raw = output_dir / "raw" / f"run-{run['run']:04d}.log"
                status.write_text(f"running {run['run']} of {len(plan)}\n")
                applied_binding = ("none" if run["condition"] == "baseline"
                                   else run["cpu_binding"])
                print(f"[{run['run']}/{len(plan)}] {run['condition']} "
                      f"cpu_binding={applied_binding} {shlex.join(command)}", flush=True)
                started = utc_now()
                process_start = time.monotonic_ns()
                with raw.open("w") as file:
                    run_command(command, file, budget_timeout(deadline, TIMEOUT_SECONDS),
                                env=worker_environment(run, physical_cpu_ids))
                finished = utc_now()
                process_wall_ns = time.monotonic_ns() - process_start
                summary, rows = parse_result(raw, run, physical_cpu_ids)
                if TT_DIAGNOSTICS and not VICTIM_TEST and run["threads"] == 1:
                    key = run["comparison_id"]
                    outcomes = [(r["bestmove"], r["raw_score"], r["nodes"]) for r in rows]
                    if key in single_worker_results:
                        previous = single_worker_results.pop(key)
                        for position, (before, after) in enumerate(zip(previous, outcomes), 1):
                            if before != after:
                                raise ValueError(f"Single-worker results changed in {key}, position {position}: {before} vs {after}")
                    else:
                        single_worker_results[key] = outcomes
                summary["process_wall_ns"] = process_wall_ns
                provenance = dict(binary_sha256=digest, raw_log=str(raw.relative_to(output_dir)))
                if VICTIM_TEST:
                    provenance["victim_policy"] = victim_policy(run["condition"])
                positions.writerows({**run, **row, **provenance} for row in rows)
                position_file.flush()
                runs.writerow({**run, **summary, **provenance, "started_utc": started,
                               "finished_utc": finished, "command": shlex.join(command)})
                run_file.flush()
                write_comparisons(output_dir)
                write_position_comparisons(output_dir)
                print(f"  elapsed {summary['wall_ns'] / 1e9:.6f} s; CPU {summary['cpu_ns'] / 1e9:.6f} s", flush=True)
        selfplay_result = "disabled"
        if SELFPLAY_ENABLED:
            default_bucket = WEISS_DEFAULT["bucket_size"]
            default_cap = WEISS_DEFAULT["cap_helpers"]
            baseline_binary = binaries["baseline", default_bucket, default_cap][0]
            candidate_binary = binaries["candidate", default_bucket, default_cap][0]
            selfplay_result = run_selfplay_smoke(
                output_dir, baseline_binary, candidate_binary, deadline)
    except (Exception, KeyboardInterrupt) as error:
        write_comparisons(output_dir)
        write_position_comparisons(output_dir)
        label = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        if isinstance(error, subprocess.TimeoutExpired):
            label = "timed-out"
        if isinstance(error, BudgetExpired) or (deadline is not None and time.monotonic() >= deadline):
            label = "budget-limited"
        status.write_text(f"{label}: {error}\n")
        update_record(
            label,
            results=f"{benchmark_results_text(output_dir)}\n\nStop reason: {error}",
            conclusion="Incomplete experiment. Interpret only completed, validated runs and record the stop reason before drawing conclusions.",
        )
        raise
    status.write_text(f"complete {utc_now()}; self-play {selfplay_result}\n")
    result_text = benchmark_results_text(output_dir) + f"\n\nSelf-play smoke: {selfplay_result}."
    update_record("complete", results=result_text)
    print(f"Completed {len(plan) // 2} paired comparisons. Results: {output_dir}", flush=True)


def round_robin_openings(path):
    """Choose the first N distinct EPD positions and save those exact inputs."""
    openings = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if (len(fields) < 4 or fields[0].count("/") != 7 or fields[1] not in ("w", "b")
                or not re.fullmatch(r"-|[KQkq]+", fields[2])
                or not re.fullmatch(r"-|[a-h][36]", fields[3])):
            raise ValueError(f"Invalid EPD position in {path}: {line}")
        position = " ".join(fields[:4])
        if position not in openings:
            openings.append(position)
        if len(openings) == OPENING_PAIRS_PER_MATCHUP:
            return openings
    raise ValueError(f"{path} needs at least {OPENING_PAIRS_PER_MATCHUP} distinct positions")


def round_robin_match_command(fastchess, first, second, binaries, output, rounds):
    command = [fastchess]
    for bucket in (first, second):
        command.extend(["-engine", f"name=Weiss-b{bucket}", "proto=uci",
                        f"cmd={binaries[bucket]}", "restart=on"])
    command.extend(["-each", "proto=uci", f"tc={ROUND_ROBIN_TIME_CONTROL}",
                    f"option.Threads={ROUND_ROBIN_THREADS}", f"option.Hash={ROUND_ROBIN_HASH_MB}",
                    "-concurrency", "1", "-rounds", str(rounds), "-repeat",
                    "-openings", f"file={output / 'openings.epd'}", "format=epd", "order=sequential",
                    "-pgnout", f"file={output / f'b{first}-vs-b{second}.pgn'}", "append=false",
                    "-autosaveinterval", "0"])
    return command


def round_robin_match_result(path, first, second, openings):
    """Validate the two colors for each actual opening in the PGN."""
    first_name, second_name = f"Weiss-b{first}", f"Weiss-b{second}"
    scores = {"1-0": 1, "0-1": 0, "1/2-1/2": 0.5}
    pairs = {opening: {} for opening in openings}
    start_position = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
    for game in pgn_result_headers(path):
        opening = " ".join(game.get("FEN", start_position).split()[:4])
        white, black, result = game["White"], game["Black"], game["Result"]
        if (opening not in pairs or {white, black} != {first_name, second_name}
                or result not in scores or white in pairs[opening]
                or game.get("Termination", "").lower() in {"abandoned", "unterminated", "rules infraction", "illegal move"}):
            raise ValueError(f"Unexpected game in {path}: {white} vs {black}, {result}, {opening}")
        pairs[opening][white] = scores[result] if white == first_name else 1 - scores[result]
    if any(len(pair) != 2 for pair in pairs.values()):
        raise ValueError(f"Missing color-swapped opening games in {path}")

    pair_points = [sum(pair.values()) for pair in pairs.values()]
    points = sum(pair_points)
    low, high = "", ""
    if len(openings) >= 10:
        # Resample opening pairs as a unit, preserving dependence between colors.
        rng = random.Random(ORDER_SEED)
        estimates = sorted(50 * sum(rng.choices(pair_points, k=len(openings))) / len(openings)
                           for _ in range(4000))
        low, high = round(estimates[100], 2), round(estimates[3899], 2)
    game_points = [score for pair in pairs.values() for score in pair.values()]
    return dict(first=first, second=second, opening_pairs=len(openings), games=len(game_points),
                first_wins=game_points.count(1), draws=game_points.count(0.5),
                second_wins=game_points.count(0), first_points=points,
                second_points=len(game_points) - points,
                first_score_pct=round(100 * points / len(game_points), 2),
                first_bootstrap_95_low_pct=low, first_bootstrap_95_high_pct=high)


def run_round_robin(opening_path, dry_run):
    buckets = ROUND_ROBIN_BUCKET_SIZES
    if (len(buckets) < 2 or len(set(buckets)) != len(buckets)
            or any(type(bucket) is not int or bucket not in (1, 2, 4, 8) for bucket in buckets)):
        raise ValueError("ROUND_ROBIN_BUCKET_SIZES must contain at least two distinct supported sizes")
    if type(OPENING_PAIRS_PER_MATCHUP) is not int or OPENING_PAIRS_PER_MATCHUP < 1:
        raise ValueError("OPENING_PAIRS_PER_MATCHUP must be a positive integer")
    if (type(ROUND_ROBIN_THREADS) is not int or not 1 <= ROUND_ROBIN_THREADS <= 2048
            or type(ROUND_ROBIN_HASH_MB) is not int or ROUND_ROBIN_HASH_MB < 2
            or not ROUND_ROBIN_TIME_CONTROL.strip()):
        raise ValueError("Invalid round robin threads, hash, or time control")
    if hasattr(os, "sched_getaffinity") and ROUND_ROBIN_THREADS > len(os.sched_getaffinity(0)):
        raise ValueError("Round robin thread count exceeds the number of allowed CPUs")
    openings = round_robin_openings(opening_path.expanduser())
    matchups = list(combinations(buckets, 2))
    print(f"{len(matchups)} matchups, {2 * len(openings)} games each, "
          f"{2 * len(openings) * len(matchups)} games total")
    output = (Path("<run-directory>") if dry_run else
              experiment_directory(ROOT / "results" / "experiments", ROUND_ROBIN_SLUG))
    binaries = {bucket: output / "bin" / f"weiss-b{bucket}" for bucket in buckets}
    fastchess = find_fastchess()
    if dry_run and not fastchess:
        fastchess = FASTCHESS_CLI
    if not fastchess:
        raise ValueError(f"Cannot find required round robin dependency {FASTCHESS_CLI!r}")
    builds = [build_command(bucket, binary) for bucket, binary in binaries.items()]
    matches = [round_robin_match_command(fastchess, first, second, binaries, output, len(openings))
               for first, second in matchups]
    if dry_run:
        for command in builds + matches:
            print(shlex.join(command))
        return

    deadline = time.monotonic() + TIME_BUDGET_HOURS * 3600
    (output / "bin").mkdir(parents=True)
    (output / "openings.epd").write_text("\n".join(openings) + "\n", encoding="utf-8")
    (output / "build-commands.txt").write_text("\n".join(map(shlex.join, builds)) + "\n")
    (output / "match-commands.txt").write_text("\n".join(map(shlex.join, matches)) + "\n")
    status = output / "status.txt"
    status.write_text("running\n")
    print(f"Results: {output}", flush=True)
    try:
        record_metadata(output, deadline, include_fastchess=True)
        settings = {"Bucket sizes": buckets, "Threads per engine": ROUND_ROBIN_THREADS,
                    "Hash MiB": ROUND_ROBIN_HASH_MB, "Time control": ROUND_ROBIN_TIME_CONTROL,
                    "Opening pairs per matchup": len(openings)}
        points = dict.fromkeys(buckets, 0.0)
        for bucket, command in zip(buckets, builds):
            with (output / f"build-b{bucket}.log").open("w") as log:
                run_command(command, log, budget_timeout(deadline))
        with (output / "head-to-head.csv").open("w", newline="") as file:
            writer = None
            for (first, second), command in zip(matchups, matches):
                name = f"b{first}-vs-b{second}"
                print(f"Playing {name}", flush=True)
                with (output / f"{name}.log").open("w") as log:
                    run_command(command, log, budget_timeout(deadline),
                                env=worker_environment({"cpu_binding": "none"}, []))
                row = round_robin_match_result(output / f"{name}.pgn", first, second, openings)
                if writer is None:
                    writer = csv.DictWriter(file, fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                file.flush()
                points[first] += row["first_points"]
                points[second] += row["second_points"]
        standings = sorted(points.items(), key=lambda item: (-item[1], item[0]))
        games = 2 * len(openings) * (len(buckets) - 1)
        with (output / "standings.csv").open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["rank", "bucket_size", "points", "games", "score_pct"])
            for bucket, score in standings:
                rank = 1 + sum(other > score for other in points.values())
                writer.writerow([rank, bucket, score, games, round(100 * score / games, 2)])
                print(f"{rank}. Bucket {bucket}: {score:g}/{games}", flush=True)
        write_experiment_md(output, title="TT bucket layout round robin",
                            question="How do TT bucket layouts score in timed games?",
                            hypothesis="Layouts may differ in playing strength.", settings=settings,
                            controls=["Same source, Hash, Threads, time control and opening pairs.",
                                      "One game at a time, with colors swapped for each opening."],
                            source_commit=capture(["git", "rev-parse", "HEAD"], required=True),
                            source_status=capture(["git", "status", "--short"], required=True),
                            binaries=[(binary.name, hashlib.sha256(binary.read_bytes()).hexdigest())
                                      for binary in binaries.values()],
                            status="complete", results="See standings.csv and head-to-head.csv.",
                            limitations=["Small match scores are tentative. With at least ten opening pairs, "
                                         "head-to-head includes approximate paired bootstrap intervals."])
        status.write_text(f"complete {utc_now()}\n")
    except BaseException as error:
        status.write_text(f"interrupted or failed {utc_now()}: {type(error).__name__}: {error}\n")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan only; no files, builds, or benchmarks")
    parser.add_argument("--round-robin", metavar="OPENINGS.EPD", type=Path,
                        help="Play a timed TT layout tournament using this EPD opening book")
    args = parser.parse_args()
    if (type(TIME_BUDGET_HOURS) not in (int, float) or not math.isfinite(TIME_BUDGET_HOURS)
            or not 0 < TIME_BUDGET_HOURS <= 24):
        raise ValueError("TIME_BUDGET_HOURS must be positive and at most 24 hours")
    if args.round_robin is not None and VICTIM_TEST:
        parser.error("The victim-policy preset is a fixed-depth benchmark")
    if args.round_robin is not None:
        run_round_robin(args.round_robin, args.dry_run)
        return
    if type(CHECK_THREAD_AFFINITY) is not bool:
        raise ValueError("CHECK_THREAD_AFFINITY must be True or False")
    if (type(AFFINITY_CHECK_TIMEOUT_SECONDS) not in (int, float)
            or AFFINITY_CHECK_TIMEOUT_SECONDS <= 0):
        raise ValueError("AFFINITY_CHECK_TIMEOUT_SECONDS must be positive")
    plan = make_experiment_plan()
    validate_paired_plan(plan)
    physical_cpu_ids = resolve_cpu_binding(plan)
    build_specs = {(run["condition"], run["bucket_size"], run["cap_helpers"])
                   for run in plan}
    if SELFPLAY_ENABLED:
        default_bucket = WEISS_DEFAULT["bucket_size"]
        default_cap = WEISS_DEFAULT["cap_helpers"]
        build_specs.update({("baseline", default_bucket, default_cap),
                            ("candidate", default_bucket, default_cap)})
    print(f"{len(plan) // 2} paired comparisons ({len(plan)} benchmark runs) x 50 positions; "
          f"{len(build_specs)} build(s).")
    print(f"Budget: {TIME_BUDGET_HOURS:g} h; per-benchmark timeout: {TIMEOUT_SECONDS} s.")
    print("Stops at the first timeout or budget limit. Complete 50-position benchmarks are saved; "
          "the final configuration block may be incomplete.")
    if physical_cpu_ids:
        requested_threads = max(run["threads"] for run in plan
                                if run["cpu_binding"] == "physical")
        print(f"Physical-core binding: {len(physical_cpu_ids)} available cores; "
              f"using CPU IDs {physical_cpu_ids[:requested_threads]} for up to {requested_threads} workers.")
    if args.dry_run:
        for condition, bucket, cap in sorted(build_specs):
            binary = Path('<run-directory>') / 'bin' / condition / f'weiss-b{bucket}-cap{cap}'
            source_root = (f"<baseline-worktree:{BASELINE_GIT_REF}>"
                           if condition == "baseline" and BASELINE_GIT_REF else ROOT)
            print(shlex.join(build_command(bucket, binary, cap, source_root,
                                           condition=condition)))
        if CHECK_THREAD_AFFINITY:
            for bucket, cap, threads in affinity_check_specs(plan):
                binary = Path('<run-directory>') / 'bin' / 'candidate' / f'weiss-b{bucket}-cap{cap}'
                cpus = physical_cpu_ids[:threads]
                print(f"affinity-check cpus={cpus}: " + shlex.join(
                    [str(binary), "affinity-check", str(threads)]))
        for run in plan:
            binary = (Path("<run-directory>") / "bin" / run["condition"]
                      / f"weiss-b{run['bucket_size']}-cap{run['cap_helpers']}")
            print(f"run={run['run']} config={run['config_name']} pair={run['comparison_id']} "
                  f"condition={run['condition']} repetition={run['repetition']} "
                  f"cpu_binding={'none' if run['condition'] == 'baseline' else run['cpu_binding']} "
                  + shlex.join(bench_command(run, binary)))
        if SELFPLAY_ENABLED:
            print(f"after benchmarks: {SELFPLAY_GAMES}-game color-swapped self-play smoke; "
                  "fastchess is required before a real run starts.")
        return
    if (type(SELFPLAY_GAMES) is not int or SELFPLAY_GAMES < 2 or SELFPLAY_GAMES % 2
            or type(SELFPLAY_THREADS) is not int or SELFPLAY_THREADS < 1
            or type(SELFPLAY_HASH_MB) is not int or SELFPLAY_HASH_MB < 1
            or not SELFPLAY_TIME_CONTROL.strip()):
        raise ValueError("Invalid self-play smoke settings")
    if SELFPLAY_ENABLED and SELFPLAY_REQUIRED and find_fastchess() is None:
        raise ValueError(f"Cannot find required self-play dependency {FASTCHESS_CLI!r}")
    if hasattr(os, "sched_getaffinity") and max(r["threads"] for r in plan) > len(os.sched_getaffinity(0)):
        raise ValueError("Requested threads exceed this process's allowed CPUs; check your allocation")
    untracked_src = capture(["git", "ls-files", "--others", "--exclude-standard", "--", "src"], required=True)
    untracked_build_inputs = [name for name in untracked_src.splitlines()
                              if Path(name).suffix.lower() != ".md"]
    if untracked_build_inputs:
        raise ValueError("Untracked build inputs under src/: git-add source changes before recording an experiment: "
                         + ", ".join(untracked_build_inputs))
    output_dir = experiment_directory(ROOT / "results" / "experiments", EXPERIMENT_SLUG)
    deadline = time.monotonic() + TIME_BUDGET_HOURS * 3600
    try:
        execute(plan, output_dir, deadline, physical_cpu_ids)
    except (BudgetExpired, subprocess.TimeoutExpired):
        print("Time limit reached. Completed benchmarks remain in runs.csv/positions.csv; "
              "unfinished runs are excluded from the CSVs, and their logs are retained.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; completed results and raw logs were retained.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Experiment stopped: {error}", file=sys.stderr)
        sys.exit(1)
