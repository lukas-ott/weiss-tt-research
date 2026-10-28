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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ======================== EDIT EXPERIMENT SETTINGS HERE ========================
BUCKET_SIZES = [2]                 # Supported: 1, 2, 4, 8. One binary per size.
DEPTHS = [12]
THREAD_COUNTS = [1, 2, 4, 8]        # Server example: [1, 2, 4, 8, 16, 32, 48, 64, 96, 128, 192]
HASH_SIZES_MB = [64]                # MiB; even values on Linux, at least 2.
REPETITIONS = 5                    # Each combination is run this many times.
ORDER_SEED = 2026                  # Reproducibly shuffle combinations per repetition.
CC = "gcc"                        # Compiler used for every bucket variant.
TIMEOUT_SECONDS = None             # Optional limit per complete 50-position bench.
# CPU/NUMA policy is inherited from your shell or scheduler; no automatic binding.
# ==============================================================================

POSITION_FIELDS = ["position", "depth", "wall_ns", "cpu_ns", "main_nodes", "nodes",
                   "bestmove", "score_type", "score", "raw_score"]
RUN_FIELDS = ["run", "repetition", "bucket_size", "depth", "threads", "hash_mb"]
PROVENANCE_FIELDS = ["binary_sha256", "raw_log"]
OVERALL = re.compile(r"^OVERALL:\s+(\d+)\s+ms\s+(\d+)\s+nodes\s+(\d+)\s+nps\s*$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def make_plan():
    dimensions = [("BUCKET_SIZES", BUCKET_SIZES, 1, 8), ("DEPTHS", DEPTHS, 1, 100),
                  ("THREAD_COUNTS", THREAD_COUNTS, 1, 2048),
                  ("HASH_SIZES_MB", HASH_SIZES_MB, 2, None)]
    for name, values, minimum, maximum in dimensions:
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"{name} must be a nonempty list")
        if any(type(n) is not int or n < minimum or (maximum is not None and n > maximum)
               for n in values) or len(values) != len(set(values)):
            raise ValueError(f"Invalid or duplicate values in {name}")
    if any(n not in (1, 2, 4, 8) for n in BUCKET_SIZES):
        raise ValueError("BUCKET_SIZES supports only 1, 2, 4, 8")
    if sys.platform.startswith("linux") and any(n % 2 for n in HASH_SIZES_MB):
        raise ValueError("HASH_SIZES_MB must be even on Linux")
    if type(REPETITIONS) is not int or REPETITIONS < 1 or type(ORDER_SEED) is not int:
        raise ValueError("REPETITIONS must be positive and ORDER_SEED must be an integer")
    if not isinstance(CC, str) or not shlex.split(CC):
        raise ValueError("CC must name a compiler")
    if TIMEOUT_SECONDS is not None and (type(TIMEOUT_SECONDS) not in (int, float)
                                       or not math.isfinite(TIMEOUT_SECONDS) or TIMEOUT_SECONDS <= 0):
        raise ValueError("TIMEOUT_SECONDS must be positive or None")
    combinations = list(itertools.product(BUCKET_SIZES, DEPTHS, THREAD_COUNTS, HASH_SIZES_MB))
    rng = random.Random(ORDER_SEED)
    plan = []
    for repetition in range(1, REPETITIONS + 1):
        block = combinations.copy()
        rng.shuffle(block)
        for bucket, depth, threads, memory in block:
            plan.append(dict(zip(RUN_FIELDS, [len(plan) + 1, repetition, bucket, depth, threads, memory])))
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
    if not {"bucket_size", "depth", "threads", "hash_mb", "entry_bytes", "bucket_bytes", "buckets"} <= config.keys():
        raise ValueError(f"Missing benchmark configuration fields in {path}")
    if any(config.get(key) != expected[key] for key in ("bucket_size", "depth", "threads", "hash_mb")):
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
    summary = {key: sum(row[key] for row in positions)
               for key in ("wall_ns", "cpu_ns", "main_nodes", "nodes")}
    if any(int(totals[0].get(key, -1)) != summary[key] for key in ("wall_ns", "cpu_ns")):
        raise ValueError(f"Clock totals do not match position rows in {path}")
    elapsed_ms, nodes, nps = overall[0]
    if nodes != summary["nodes"] or elapsed_ms != max(1, summary["wall_ns"] // 1_000_000):
        raise ValueError(f"OVERALL does not match position rows in {path}")
    summary.update(elapsed_ms=elapsed_ms, nps=nps,
                   **{key: config[key] for key in ("entry_bytes", "bucket_bytes", "buckets")})
    return summary, positions


def build_command(bucket, binary):
    return ["make", "-C", "src", "basic", f"BUCKET_SIZE={bucket}", f"CC={CC}", f"EXE={binary}"]


def bench_command(run, binary):
    return [str(binary), "bench", str(run["depth"]), str(run["threads"]), str(run["hash_mb"])]


def record_metadata(output_dir):
    commit = capture(["git", "rev-parse", "HEAD"], required=True)
    status = capture(["git", "status", "--short"], required=True)
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    (output_dir / "source.patch").write_bytes(patch)
    shutil.copyfile(Path(__file__), output_dir / "runner.py")
    details = [f"started_utc: {utc_now()}", f"hostname: {platform.node()}",
               f"platform: {platform.platform()}", f"machine: {platform.machine()}",
               f"python: {sys.version}", f"source_commit: {commit}",
               f"source_status_before_run:\n{status or '(clean)'}",
               f"runner_command: {shlex.join(sys.argv)}", f"order_seed: {ORDER_SEED}",
               f"timeout_seconds: {TIMEOUT_SECONDS}",
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


def execute(plan, output_dir):
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "raw").mkdir()
    (output_dir / "bin").mkdir()
    status = output_dir / "status.txt"
    status.write_text("building\n")
    print(f"Results: {output_dir}", flush=True)
    try:
        record_metadata(output_dir)
        with (output_dir / "plan.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_FIELDS)
            writer.writeheader()
            writer.writerows(plan)
        binaries = {}
        with (output_dir / "build-commands.txt").open("w") as commands:
            for bucket in BUCKET_SIZES:
                binary = output_dir / "bin" / f"weiss-b{bucket}"
                command = build_command(bucket, binary)
                commands.write(shlex.join(command) + "\n")
                commands.flush()
                print(f"Building bucket size {bucket}", flush=True)
                log = output_dir / f"build-b{bucket}.log"
                with log.open("w") as file:
                    subprocess.run(command, cwd=ROOT, stdout=file, stderr=subprocess.STDOUT, check=True)
                binaries[bucket] = (binary, hashlib.sha256(binary.read_bytes()).hexdigest())
        summary_fields = ["wall_ns", "cpu_ns", "main_nodes", "nodes", "elapsed_ms", "nps",
                          "entry_bytes", "bucket_bytes", "buckets"]
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
                binary, digest = binaries[run["bucket_size"]]
                command = bench_command(run, binary)
                raw = output_dir / "raw" / f"run-{run['run']:04d}.log"
                status.write_text(f"running {run['run']} of {len(plan)}\n")
                print(f"[{run['run']}/{len(plan)}] {shlex.join(command)}", flush=True)
                started = utc_now()
                with raw.open("w") as file:
                    subprocess.run(command, cwd=ROOT, stdout=file, stderr=subprocess.STDOUT,
                                   check=True, timeout=TIMEOUT_SECONDS)
                finished = utc_now()
                summary, rows = parse_result(raw, run)
                provenance = dict(binary_sha256=digest, raw_log=str(raw.relative_to(output_dir)))
                positions.writerows({**run, **row, **provenance} for row in rows)
                position_file.flush()
                runs.writerow({**run, **summary, **provenance, "started_utc": started,
                               "finished_utc": finished, "command": shlex.join(command)})
                run_file.flush()
                print(f"  elapsed {summary['wall_ns'] / 1e9:.6f} s; CPU {summary['cpu_ns'] / 1e9:.6f} s", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        status.write_text(f"{'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'}: {error}\n")
        raise
    status.write_text(f"complete {utc_now()}\n")
    print(f"Completed {len(plan)} runs. Results: {output_dir}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan only; no files, builds, or benchmarks")
    args = parser.parse_args()
    plan = make_plan()
    print(f"{len(plan)} benchmark runs x 50 positions; {len(BUCKET_SIZES)} build(s).")
    if args.dry_run:
        for bucket in BUCKET_SIZES:
            print(shlex.join(build_command(bucket, Path('<run-directory>') / 'bin' / f'weiss-b{bucket}')))
        for run in plan:
            print(f"run={run['run']} repetition={run['repetition']} "
                  + shlex.join(bench_command(run, Path('<run-directory>') / 'bin' / f"weiss-b{run['bucket_size']}")))
        return
    if hasattr(os, "sched_getaffinity") and max(THREAD_COUNTS) > len(os.sched_getaffinity(0)):
        raise ValueError("Requested threads exceed this process's allowed CPUs; check your allocation")
    if capture(["git", "ls-files", "--others", "--exclude-standard", "--", "src"], required=True):
        raise ValueError("Untracked files under src/: git-add source changes before recording an experiment")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    execute(plan, ROOT / "results" / "experiments" / run_id)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; completed results and raw logs were retained.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Experiment stopped: {error}", file=sys.stderr)
        sys.exit(1)
