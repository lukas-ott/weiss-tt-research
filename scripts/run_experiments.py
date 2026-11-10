#!/usr/bin/env python3
"""Build Weiss variants and run the existing 50-position benchmark sequentially."""

import argparse
import csv
import hashlib
import itertools
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
# Run the baseline first, then the depth/hash sweep. Helpers keep their original policy.
BUCKET_SIZES = [2]                 # Supported: 1, 2, 4, 8. One binary per size.
DEPTHS = [15]
THREAD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 192]
HASH_SIZES_MB = [64]                # MiB; even values on Linux, at least 2.
REPETITIONS = 30                   # Each combination is run this many times.
ORDER_SEED = 2026                  # Shuffle configurations within each repetition.
CC = "gcc"
TIMEOUT_SECONDS = 300              # Whole 50-position process, including TT clearing; None disables.
EXPERIMENT_SLUG = "tt-benchmark-sweep"  # Used in results/experiments/YYYYMMDD_<slug>/.

# Applies to every invocation. Completed runs are saved before the budget expires.
TIME_BUDGET_HOURS = 6.5             # Includes builds and process startup/TT clearing.
SWEEP_DEPTHS = list(range(15, 31))   # Ascending depths; [] skips this sweep.
SWEEP_THREADS = [1, 32, 128, 192]
SWEEP_HASH_SIZES_MB = [2, 64, 512]
SWEEP_REPETITIONS = 5               # Sweep uses original, uncapped helpers only.
ROOT_MOVE_ROTATIONS = [0]            # 0 is original; one-off runner requests [0, 1].
# CPU/NUMA policy is inherited from your shell or scheduler; no automatic binding.
# ==============================================================================

POSITION_FIELDS = ["position", "depth", "wall_ns", "cpu_ns", "main_nodes", "nodes",
                   "bestmove", "score_type", "score", "raw_score",
                   "helper_max_started_iteration", "helper_max_completed_iteration",
                   "helper_nodes_beyond_depth"]
RUN_FIELDS = ["run", "repetition", "bucket_size", "depth", "threads", "hash_mb",
              "cap_helpers", "rotate_root_moves", "phase"]
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
                        controls, source_commit, source_status, runner_file,
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
              f"- Runner snapshot: `{runner_file}`"]
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
        key = tuple(int(row.get(name) or 0) for name in dimensions)
        group = groups.setdefault(key, {"wall": [], "nodes": [], "main_nodes": []})
        group["wall"].append(int(row["wall_ns"]) / 1e9)
        group["nodes"].append(int(row["nodes"]))
        group["main_nodes"].append(int(row["main_nodes"]))
    lines = [f"Completed benchmark runs: {len(rows)}.", "",
             "| Bucket | Depth | Workers | TT MiB | Cap | Rotation | Mean wall s | Mean nodes | Mean main nodes |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key, values in sorted(groups.items()):
        means = [sum(values[name]) / len(values[name])
                 for name in ("wall", "nodes", "main_nodes")]
        lines.append("| " + " | ".join(map(str, key)) + " | "
                     + f"{means[0]:.4f} | {means[1]:.0f} | {means[2]:.0f} |")
    return "\n".join(lines)


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


def make_plan(*, buckets=None, depths=None, threads=None, hashes=None, caps=None,
              rotations=None, repetitions=None, phase="baseline"):
    buckets = BUCKET_SIZES if buckets is None else buckets
    depths = DEPTHS if depths is None else depths
    threads = THREAD_COUNTS if threads is None else threads
    hashes = HASH_SIZES_MB if hashes is None else hashes
    caps = [0] if caps is None else caps
    rotations = ROOT_MOVE_ROTATIONS if rotations is None else rotations
    repetitions = REPETITIONS if repetitions is None else repetitions
    dimensions = [("BUCKET_SIZES", buckets, 1, 8), ("DEPTHS", depths, 1, 100),
                  ("THREAD_COUNTS", threads, 1, 2048),
                  ("HASH_SIZES_MB", hashes, 2, None), ("CAP_HELPERS", caps, 0, 1),
                  ("ROOT_MOVE_ROTATIONS", rotations, 0, 1)]
    for name, values, minimum, maximum in dimensions:
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"{name} must be a nonempty list")
        if any(type(n) is not int or n < minimum or (maximum is not None and n > maximum)
               for n in values) or len(values) != len(set(values)):
            raise ValueError(f"Invalid or duplicate values in {name}")
    if any(n not in (1, 2, 4, 8) for n in buckets):
        raise ValueError("BUCKET_SIZES supports only 1, 2, 4, 8")
    if sys.platform.startswith("linux") and any(n % 2 for n in hashes):
        raise ValueError("HASH_SIZES_MB must be even on Linux")
    if type(repetitions) is not int or repetitions < 1 or type(ORDER_SEED) is not int:
        raise ValueError("REPETITIONS must be positive and ORDER_SEED must be an integer")
    if not isinstance(CC, str) or not shlex.split(CC):
        raise ValueError("CC must name a compiler")
    if TIMEOUT_SECONDS is not None and (type(TIMEOUT_SECONDS) not in (int, float)
                                       or not math.isfinite(TIMEOUT_SECONDS) or TIMEOUT_SECONDS <= 0):
        raise ValueError("TIMEOUT_SECONDS must be positive or None")
    combinations = list(itertools.product(buckets, depths, threads, hashes, caps, rotations))
    rng = random.Random(ORDER_SEED)
    plan = []
    for repetition in range(1, repetitions + 1):
        block = combinations.copy()
        rng.shuffle(block)
        for bucket, depth, workers, memory, cap, rotate in block:
            plan.append(dict(zip(RUN_FIELDS, [len(plan) + 1, repetition, bucket, depth,
                                               workers, memory, cap, rotate, phase])))
    return plan


def make_experiment_plan():
    if (not isinstance(SWEEP_DEPTHS, (list, tuple))
            or any(type(d) is not int for d in SWEEP_DEPTHS)
            or list(SWEEP_DEPTHS) != sorted(set(SWEEP_DEPTHS))):
        raise ValueError("SWEEP_DEPTHS must contain increasing, unique integer depths")
    if any(d < 15 or d > 100 for d in SWEEP_DEPTHS):
        raise ValueError("SWEEP_DEPTHS must be between 15 and 100")
    plan = make_plan()
    for depth in SWEEP_DEPTHS:
        plan += make_plan(depths=[depth], threads=SWEEP_THREADS,
                          hashes=SWEEP_HASH_SIZES_MB, caps=[0],
                          repetitions=SWEEP_REPETITIONS, phase="depth-sweep")
    for number, run in enumerate(plan, 1):
        run["run"] = number
    return plan


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


def build_command(bucket, binary, cap_helpers=0):
    return ["make", "-C", "src", "basic", f"BUCKET_SIZE={bucket}",
            f"CPPFLAGS=-DBENCH_CAP_HELPERS={cap_helpers}", f"CC={CC}", f"EXE={binary}"]


def bench_command(run, binary):
    command = [str(binary), "bench", str(run["depth"]), str(run["threads"]), str(run["hash_mb"])]
    # Zero is the historical default; omit it to keep older command expectations valid.
    if run.get("rotate_root_moves", 0):
        command.append(str(run["rotate_root_moves"]))
    return command


def record_metadata(output_dir, deadline=None, runner_file=None):
    commit = capture(["git", "rev-parse", "HEAD"], required=True)
    status = capture(["git", "status", "--short"], required=True)
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    (output_dir / "source.patch").write_bytes(patch)
    entrypoint = Path(__file__) if runner_file is None else Path(runner_file)
    shutil.copyfile(entrypoint, output_dir / "runner.py")
    if entrypoint.resolve() != Path(__file__).resolve():
        shutil.copyfile(Path(__file__), output_dir / "run_experiments.py")
    details = [f"started_utc: {utc_now()}", f"hostname: {platform.node()}",
               f"platform: {platform.platform()}", f"machine: {platform.machine()}",
               f"python: {sys.version}", f"source_commit: {commit}",
               f"source_status_before_run:\n{status or '(clean)'}",
               f"runner_command: {shlex.join(sys.argv)}", f"order_seed: {ORDER_SEED}",
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


def execute(plan, output_dir, deadline=None, runner_file=None, experiment_record=None):
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "raw").mkdir()
    (output_dir / "bin").mkdir()
    status = output_dir / "status.txt"
    status.write_text("building\n")
    print(f"Results: {output_dir}", flush=True)
    record = experiment_record or {}
    source = {"commit": "not recorded", "status": "not recorded"}
    built_binaries = []

    def update_record(state, results=None, conclusion=None):
        settings = {
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
        }
        write_experiment_md(
            output_dir,
            title=record.get("title", "Weiss benchmark experiment"),
            question=record.get("question", "How do the selected settings affect benchmark time to depth?"),
            hypothesis=record.get("hypothesis", "Additional workers may reduce wall time at first, with diminishing returns at higher counts."),
            settings=settings,
            controls=record.get("controls", [
                "The same ordered 50-position benchmark is used for every run.",
                "Runs execute sequentially with one executable per bucket size.",
                f"Configuration order is randomized with seed {ORDER_SEED}.",
                "CPU and NUMA placement are inherited from the invoking shell or scheduler.",
            ]),
            source_commit=source["commit"], source_status=source["status"],
            runner_file=Path(runner_file or __file__).name,
            binaries=built_binaries, status=state,
            results=results or benchmark_results_text(output_dir),
            conclusion=conclusion or "Pending manual interpretation. Descriptive means do not establish statistical significance or causality.",
            limitations=record.get("limitations", [
                "Parallel searches may visit different numbers of nodes and may not be deterministic.",
                "Recorded means summarize completed runs only; incomplete configurations are not comparable as full repetitions.",
                "This benchmark measures time to a requested depth, not playing strength.",
            ]),
        )

    try:
        source.update(record_metadata(output_dir, deadline, runner_file) or {})
        update_record("building")
        with (output_dir / "plan.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_FIELDS)
            writer.writeheader()
            writer.writerows(plan)
        binaries = {}
        with (output_dir / "build-commands.txt").open("w") as commands:
            for bucket, cap in sorted({(r["bucket_size"], r["cap_helpers"]) for r in plan}):
                binary = output_dir / "bin" / f"weiss-b{bucket}-cap{cap}"
                command = build_command(bucket, binary, cap)
                commands.write(shlex.join(command) + "\n")
                commands.flush()
                print(f"Building bucket size {bucket}, cap_helpers={cap}", flush=True)
                log = output_dir / f"build-b{bucket}-cap{cap}.log"
                with log.open("w") as file:
                    run_command(command, file, budget_timeout(deadline))
                binaries[bucket, cap] = (binary, hashlib.sha256(binary.read_bytes()).hexdigest())
                built_binaries.append((binary.name, binaries[bucket, cap][1]))
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
                binary, digest = binaries[run["bucket_size"], run["cap_helpers"]]
                command = bench_command(run, binary)
                raw = output_dir / "raw" / f"run-{run['run']:04d}.log"
                status.write_text(f"running {run['run']} of {len(plan)}\n")
                print(f"[{run['run']}/{len(plan)}] {shlex.join(command)}", flush=True)
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
                print(f"  elapsed {summary['wall_ns'] / 1e9:.6f} s; CPU {summary['cpu_ns'] / 1e9:.6f} s", flush=True)
    except (Exception, KeyboardInterrupt) as error:
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
    status.write_text(f"complete {utc_now()}\n")
    update_record("complete", conclusion=record.get(
        "conclusion", "Pending manual interpretation. Review repetitions and uncertainty before drawing a scaling conclusion."))
    print(f"Completed {len(plan)} runs. Results: {output_dir}", flush=True)


def main(plan=None, runner_file=None, experiment_slug=EXPERIMENT_SLUG, experiment_record=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan only; no files, builds, or benchmarks")
    args = parser.parse_args()
    if (type(TIME_BUDGET_HOURS) not in (int, float) or not math.isfinite(TIME_BUDGET_HOURS)
            or not 0 < TIME_BUDGET_HOURS <= 24):
        raise ValueError("TIME_BUDGET_HOURS must be positive and at most 24 hours")
    plan = make_experiment_plan() if plan is None else plan
    variants = sorted({(r["bucket_size"], r["cap_helpers"]) for r in plan})
    print(f"{len(plan)} benchmark runs x 50 positions; {len(variants)} build(s).")
    print(f"Budget: {TIME_BUDGET_HOURS:g} h; per-benchmark timeout: {TIMEOUT_SECONDS} s.")
    print("Stops at the first timeout or budget limit. Complete 50-position benchmarks are saved; "
          "the final configuration block may be incomplete.")
    if args.dry_run:
        for bucket, cap in variants:
            binary = Path('<run-directory>') / 'bin' / f'weiss-b{bucket}-cap{cap}'
            print(shlex.join(build_command(bucket, binary, cap)))
        for run in plan:
            print(f"run={run['run']} phase={run['phase']} repetition={run['repetition']} "
                  + shlex.join(bench_command(run, Path('<run-directory>') / 'bin' / f"weiss-b{run['bucket_size']}-cap{run['cap_helpers']}")))
        return
    if hasattr(os, "sched_getaffinity") and max(r["threads"] for r in plan) > len(os.sched_getaffinity(0)):
        raise ValueError("Requested threads exceed this process's allowed CPUs; check your allocation")
    untracked_src = capture(["git", "ls-files", "--others", "--exclude-standard", "--", "src"], required=True)
    untracked_build_inputs = [name for name in untracked_src.splitlines()
                              if Path(name).suffix.lower() != ".md"]
    if untracked_build_inputs:
        raise ValueError("Untracked build inputs under src/: git-add source changes before recording an experiment: "
                         + ", ".join(untracked_build_inputs))
    output_dir = experiment_directory(ROOT / "results" / "experiments", experiment_slug)
    deadline = time.monotonic() + TIME_BUDGET_HOURS * 3600
    try:
        execute(plan, output_dir, deadline, runner_file=runner_file,
                experiment_record=experiment_record)
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
