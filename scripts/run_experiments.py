#!/usr/bin/env python3
"""Build Weiss variants and run the existing 50-position benchmark sequentially."""

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
TIME_BUDGET_HOURS = 6.5             # Includes builds, bench and final self-play smoke.

# This is the reference engine code. Every candidate run is paired against it
# with identical benchmark arguments. Change only when intentionally resetting
# the research control; record the new reference in the thesis methods.
BASELINE_GIT_REF = "c55272499a460acdb7bd057ad55d495b65faaefd"

# Run a small UCI self-play smoke test after all benchmark pairs. It checks that
# both builds can play legal games; four games cannot establish playing strength.
SELFPLAY_ENABLED = True
SELFPLAY_REQUIRED = True             # Require the routine smoke check before any runs start.
CUTECHESS_CLI = "cutechess-cli"
SELFPLAY_GAMES = 4                    # Must be even; colors are swapped in pairs.
SELFPLAY_TIME_CONTROL = "1+0.1"
SELFPLAY_HASH_MB = WEISS_DEFAULT["hash_mb"]
SELFPLAY_THREADS = WEISS_DEFAULT["threads"]
# CPU/NUMA policy is inherited from your shell or scheduler; no automatic binding.
# ==============================================================================

POSITION_FIELDS = ["position", "depth", "wall_ns", "cpu_ns", "main_nodes", "nodes",
                   "bestmove", "score_type", "score", "raw_score",
                   "helper_max_started_iteration", "helper_max_completed_iteration",
                   "helper_nodes_beyond_depth"]
CONFIG_FIELDS = ["bucket_size", "depth", "threads", "hash_mb", "cap_helpers",
                 "rotate_root_moves"]
RUN_FIELDS = ["run", "comparison_id", "condition", "config_name", "repetition",
              *CONFIG_FIELDS, "phase"]
PROVENANCE_FIELDS = ["binary_sha256", "raw_log"]
OVERALL = re.compile(r"^OVERALL:\s+(\d+)\s+ms\s+(\d+)\s+nodes\s+(\d+)\s+nps\s*$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                  "rotate_root_moves")
    groups = {}
    for row in rows:
        key = (row["condition"], row["config_name"]) + tuple(
            int(row.get(name) or 0) for name in dimensions)
        group = groups.setdefault(key, {"wall": [], "nodes": [], "main_nodes": []})
        group["wall"].append(int(row["wall_ns"]) / 1e9)
        group["nodes"].append(int(row["nodes"]))
        group["main_nodes"].append(int(row["main_nodes"]))
    lines = [f"Completed benchmark runs: {len(rows)}.", "",
             "| Build | Config | Bucket | Depth | Workers | TT MiB | Cap | Rotation | Mean wall s | Mean nodes | Mean main nodes |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
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
              "cap_helpers", "rotate_root_moves", "baseline_run", "candidate_run",
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
                "hash_mb", "cap_helpers", "rotate_root_moves")}
            for label, run in (("baseline", baseline), ("candidate", candidate)):
                row[f"{label}_run"] = run["run"]
                row[f"{label}_binary_sha256"] = run["binary_sha256"]
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
              "cap_helpers", "rotate_root_moves", "position", "baseline_wall_ns",
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
                    "hash_mb", "cap_helpers", "rotate_root_moves", "position")}
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
    if sys.platform.startswith("linux") and config["hash_mb"] % 2:
        raise ValueError("hash_mb must be even on Linux")


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


def parse_result(path, expected):
    """Accept only a complete, internally consistent log for the requested run."""
    configs, headers, positions, totals, overall = [], [], [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("BENCH_CONFIG "):
            configs.append(dict(re.findall(r"(\w+)=(\d+)", line)))
        elif line.startswith("BENCH_POSITION_HEADER,"):
            headers.append(next(csv.reader([line]))[1:])
        elif line.startswith("BENCH_POSITION,"):
            values = next(csv.reader([line]))[1:]
            if len(values) != len(POSITION_FIELDS):
                raise ValueError(f"Malformed position row in {path}")
            row = dict(zip(POSITION_FIELDS, values))
            for field in POSITION_FIELDS:
                if field not in ("bestmove", "score_type"):
                    row[field] = int(row[field])
            positions.append(row)
        elif line.startswith("BENCH_TOTAL "):
            totals.append(dict(re.findall(r"(\w+)=(\d+)", line)))
        elif match := OVERALL.fullmatch(line):
            overall.append(tuple(map(int, match.groups())))
    if len(configs) != 1 or headers != [POSITION_FIELDS] or len(totals) != 1 or len(overall) != 1:
        raise ValueError(f"Missing or duplicate benchmark records in {path}")
    config = {key: int(value) for key, value in configs[0].items()}
    config.setdefault("rotate_root_moves", 0)  # Older benchmark logs used the unrotated order.
    if not {"bucket_size", "depth", "threads", "hash_mb", "entry_bytes", "bucket_bytes", "buckets", "cap_helpers", "rotate_root_moves"} <= config.keys():
        raise ValueError(f"Missing benchmark configuration fields in {path}")
    if any(config.get(key) != expected.get(key, 0) for key in ("bucket_size", "depth", "threads", "hash_mb", "cap_helpers", "rotate_root_moves")):
        raise ValueError(f"Benchmark configuration differs from requested run in {path}")
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
    summary.update(
        helper_nodes_beyond_depth=sum(row["helper_nodes_beyond_depth"] for row in positions),
        helper_max_started_iteration=max(row["helper_max_started_iteration"] for row in positions),
        helper_max_completed_iteration=max(row["helper_max_completed_iteration"] for row in positions))
    summary.update(elapsed_ms=elapsed_ms, nps=nps,
                   **{key: config[key] for key in ("entry_bytes", "bucket_bytes", "buckets")})
    return summary, positions


def build_command(bucket, binary, cap_helpers=0, source_root=ROOT):
    return ["make", "-C", str(Path(source_root) / "src"), "basic", f"BUCKET_SIZE={bucket}",
            f"CPPFLAGS=-DBENCH_CAP_HELPERS={cap_helpers}", f"CC={CC}", f"EXE={binary}"]


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


def record_metadata(output_dir, deadline=None):
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
               f"baseline_git_ref: {BASELINE_GIT_REF or '(disabled; same source for test fixture)'}",
               f"timeout_seconds: {TIMEOUT_SECONDS}",
               f"total_budget_seconds_at_metadata: {None if deadline is None else max(0, deadline - time.monotonic())}",
               f"allowed_cpus: {sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else 'unavailable'}",
               "placement: inherited from invoking shell/scheduler; no per-worker pinning"]
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


def run_command(command, file, timeout):
    """On timeout/interrupt, also stop compiler children rather than leaving jobs behind."""
    process = subprocess.Popen(command, cwd=ROOT, stdout=file, stderr=subprocess.STDOUT,
                               start_new_session=os.name == "posix")
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
    if code:
        raise subprocess.CalledProcessError(code, command)


def pgn_result_headers(path):
    """Read only PGN headers, enough to summarize the smoke match."""
    games, tags = [], {}
    header = re.compile(r'^\[([A-Za-z0-9_]+)\s+"((?:[^"\\]|\\.)*)"\]\s*$')
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = header.fullmatch(line.strip())
        if match:
            if "Result" in tags:
                games.append(tags)
                tags = {}
            tags[match.group(1)] = match.group(2)
        elif not line.strip() and "Result" in tags:
            games.append(tags)
            tags = {}
    if tags:
        games.append(tags)
    return games


def run_selfplay_smoke(output_dir, baseline_binary, candidate_binary, deadline):
    """Run a small color-swapped UCI smoke match after the search benchmark."""
    status_path = Path(output_dir) / "selfplay-status.txt"
    cutechess = shutil.which(CUTECHESS_CLI)
    if not SELFPLAY_ENABLED:
        status_path.write_text("disabled by settings\n", encoding="utf-8")
        return "disabled"
    if cutechess is None:
        message = f"skipped: cannot find {CUTECHESS_CLI!r}"
        status_path.write_text(message + "\n", encoding="utf-8")
        if SELFPLAY_REQUIRED:
            raise RuntimeError(message)
        print(f"Self-play smoke skipped: {message}", flush=True)
        return message
    if SELFPLAY_GAMES < 2 or SELFPLAY_GAMES % 2:
        raise ValueError("SELFPLAY_GAMES must be an even integer of at least 2")
    pgn_path = Path(output_dir) / "selfplay.pgn"
    command = [cutechess,
               "-engine", "name=Weiss candidate", "proto=uci", f"cmd={candidate_binary}",
               "-engine", "name=Weiss baseline", "proto=uci", f"cmd={baseline_binary}",
               "-each", "proto=uci", f"tc={SELFPLAY_TIME_CONTROL}",
               f"option.Threads={SELFPLAY_THREADS}", f"option.Hash={SELFPLAY_HASH_MB}",
               "-concurrency", "1", "-games", str(SELFPLAY_GAMES), "-repeat",
               "-pgnout", str(pgn_path)]
    (Path(output_dir) / "selfplay-command.txt").write_text(
        shlex.join(command) + "\n", encoding="utf-8")
    status_path.write_text("running\n", encoding="utf-8")
    with (Path(output_dir) / "selfplay.log").open("w", encoding="utf-8") as log:
        run_command(command, log, budget_timeout(deadline, 1800))
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


def execute(plan, output_dir, deadline=None):
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
            "Repetitions": sorted({run["repetition"] for run in plan}),
            "Timeout": TIMEOUT_SECONDS,
            "Time budget (hours)": TIME_BUDGET_HOURS,
            "Pinned baseline source": BASELINE_GIT_REF or "same source (test mode)",
            "Self-play smoke": (f"{SELFPLAY_GAMES} games at {SELFPLAY_TIME_CONTROL}; "
                                f"Hash {SELFPLAY_HASH_MB} MiB, Threads {SELFPLAY_THREADS}"
                                if SELFPLAY_ENABLED else "disabled"),
        }
        write_experiment_md(
            output_dir,
            title="Weiss paired benchmark experiment",
            question="How do changes in the current Weiss checkout affect time to depth, search work, moves and scores against the pinned source baseline?",
            hypothesis="The candidate may change runtime and search results; the direction and size of the effect are measured with matched baseline/candidate runs.",
            settings=settings,
            controls=[
                "Each baseline/candidate pair uses identical depth, threads, TT settings, and the same ordered 50 positions.",
                "Each pair uses one build from the pinned baseline source and one from the current checkout.",
                f"Pair order is randomized with seed {ORDER_SEED}; searches run sequentially.",
                "A short color-swapped self-play smoke check runs after benchmark pairs; Cute Chess CLI is required before the run starts.",
                "CPU and NUMA placement are inherited from the invoking shell or scheduler.",
            ],
            source_commit=source["commit"], source_status=source["status"],
            binaries=built_binaries, status=state,
            results=results or benchmark_results_text(output_dir),
            conclusion=conclusion or "Compare paired rows in comparisons.csv. The short self-play smoke is a protocol check, not a strength estimate.",
            limitations=[
                "Parallel searches may visit different numbers of nodes and may not be deterministic.",
                "Recorded means summarize completed runs only; incomplete configurations are not comparable as full repetitions.",
                "This benchmark measures time to a requested depth, not playing strength.",
            ],
        )

    try:
        source.update(record_metadata(output_dir, deadline) or {})
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
                    command = build_command(bucket, binary, cap, source_roots[condition])
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
        update_record("running", results="Builds completed; benchmark runs are in progress.")
        summary_fields = ["wall_ns", "cpu_ns", "main_nodes", "nodes", "elapsed_ms", "nps",
                          "entry_bytes", "bucket_bytes", "buckets", "process_wall_ns",
                          "helper_nodes_beyond_depth", "helper_max_started_iteration",
                          "helper_max_completed_iteration"]
        with (output_dir / "runs.csv").open("w", newline="") as run_file, (
                output_dir / "positions.csv").open("w", newline="") as position_file:
            runs = csv.DictWriter(run_file, fieldnames=RUN_FIELDS + ["started_utc", "finished_utc"]
                                  + summary_fields + PROVENANCE_FIELDS + ["command"])
            # Completed depth equals requested depth after validation; store it once.
            position_fields = RUN_FIELDS + [name for name in POSITION_FIELDS if name not in RUN_FIELDS]
            positions = csv.DictWriter(position_file, fieldnames=position_fields + PROVENANCE_FIELDS)
            runs.writeheader()
            positions.writeheader()
            run_file.flush()
            position_file.flush()
            for run in plan:
                binary, digest = binaries[run["condition"], run["bucket_size"], run["cap_helpers"]]
                command = bench_command(run, binary)
                raw = output_dir / "raw" / f"run-{run['run']:04d}.log"
                status.write_text(f"running {run['run']} of {len(plan)}\n")
                print(f"[{run['run']}/{len(plan)}] {run['condition']} {shlex.join(command)}", flush=True)
                started = utc_now()
                process_start = time.monotonic_ns()
                with raw.open("w") as file:
                    run_command(command, file, budget_timeout(deadline, TIMEOUT_SECONDS))
                finished = utc_now()
                process_wall_ns = time.monotonic_ns() - process_start
                summary, rows = parse_result(raw, run)
                summary["process_wall_ns"] = process_wall_ns
                provenance = dict(binary_sha256=digest, raw_log=str(raw.relative_to(output_dir)))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan only; no files, builds, or benchmarks")
    args = parser.parse_args()
    if (type(TIME_BUDGET_HOURS) not in (int, float) or not math.isfinite(TIME_BUDGET_HOURS)
            or not 0 < TIME_BUDGET_HOURS <= 24):
        raise ValueError("TIME_BUDGET_HOURS must be positive and at most 24 hours")
    plan = make_experiment_plan()
    validate_paired_plan(plan)
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
    if args.dry_run:
        for condition, bucket, cap in sorted(build_specs):
            binary = Path('<run-directory>') / 'bin' / condition / f'weiss-b{bucket}-cap{cap}'
            source_root = (f"<baseline-worktree:{BASELINE_GIT_REF}>"
                           if condition == "baseline" and BASELINE_GIT_REF else ROOT)
            print(shlex.join(build_command(bucket, binary, cap, source_root)))
        for run in plan:
            binary = (Path("<run-directory>") / "bin" / run["condition"]
                      / f"weiss-b{run['bucket_size']}-cap{run['cap_helpers']}")
            print(f"run={run['run']} config={run['config_name']} pair={run['comparison_id']} "
                  f"condition={run['condition']} repetition={run['repetition']} "
                  + shlex.join(bench_command(run, binary)))
        if SELFPLAY_ENABLED:
            print(f"after benchmarks: {SELFPLAY_GAMES}-game color-swapped self-play smoke; "
                  f"Cute Chess CLI is required before a real run starts.")
        return
    if (type(SELFPLAY_GAMES) is not int or SELFPLAY_GAMES < 2 or SELFPLAY_GAMES % 2
            or type(SELFPLAY_THREADS) is not int or SELFPLAY_THREADS < 1
            or type(SELFPLAY_HASH_MB) is not int or SELFPLAY_HASH_MB < 1
            or not SELFPLAY_TIME_CONTROL.strip()):
        raise ValueError("Invalid self-play smoke settings")
    if SELFPLAY_ENABLED and SELFPLAY_REQUIRED and shutil.which(CUTECHESS_CLI) is None:
        raise ValueError(f"Cannot find required self-play dependency {CUTECHESS_CLI!r}")
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
        execute(plan, output_dir, deadline)
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
