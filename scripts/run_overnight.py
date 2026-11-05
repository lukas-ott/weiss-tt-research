#!/usr/bin/env python3
"""Run a bounded overnight study; standard library only, no SSH or pip needed."""

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import time

import run_experiments as runner

# ======================== EDIT OVERNIGHT SETTINGS HERE ========================
HOURS = 6.5
BASELINE_REPETITIONS = 30
BASELINE_THREADS = [1, 2, 4, 8, 16, 32, 64, 128, 192]
DEPTH_THREADS = [1, 32, 128, 192]
DEPTHS = list(range(15, 31))        # Stop earlier when a balanced batch won't fit.
HASH_SIZES_MB = [2, 64, 512]       # Experimental small/reference/large TT sizes.
DEPTH_REPETITIONS = 5
CAP_HELPERS = [0, 1]              # Original and capped at requested iteration.
MAX_RUN_SECONDS = 300             # Entire 50-position benchmark, including clearing.
SAFETY_FACTOR = 1.5               # Timing estimate, not a completion guarantee.
RESERVE_SECONDS = 600
CC = "gcc"
ORDER_SEED = 2026
# Bucket size stays fixed at 2; placement is inherited and recorded, not changed.
# ==============================================================================


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def append_csv(path, rows):
    if not rows:
        return
    existed = path.exists()
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        if not existed:
            writer.writeheader()
        writer.writerows(rows)


def collect_balanced(batch, output, phase, plan):
    """Combine whole configuration blocks only; keep incomplete data in its batch."""
    runs = read_csv(batch / "runs.csv")
    found = {int(row["run"]) for row in runs}
    complete_repetitions = {
        repetition for repetition in {row["repetition"] for row in plan}
        if {row["run"] for row in plan if row["repetition"] == repetition} <= found
    }
    accepted = [row for row in runs if int(row["repetition"]) in complete_repetitions]
    existing = read_csv(output / "runs.csv")
    run_base = max((int(row["run"]) for row in existing), default=0)
    repetition_base = max((int(row["repetition"]) for row in existing), default=0)
    run_ids = {row["run"]: run_base + i for i, row in enumerate(accepted, 1)}

    def relocate(row):
        return {**row, "run": run_ids[row["run"]],
                "repetition": repetition_base + int(row["repetition"]),
                "raw_log": str(Path(batch.name) / row["raw_log"]),
                "phase": phase, "batch": batch.name,
                "batch_run": row["run"], "batch_repetition": row["repetition"]}

    append_csv(output / "runs.csv", [relocate(row) for row in accepted])
    positions = read_csv(batch / "positions.csv")
    append_csv(output / "positions.csv",
               [relocate(row) for row in positions if row["run"] in run_ids])
    return len(accepted)


def configure(depth, threads, hashes, repetitions, seed):
    runner.BUCKET_SIZES = [2]
    runner.DEPTHS = [depth]
    runner.THREAD_COUNTS = threads
    runner.HASH_SIZES_MB = hashes
    runner.CAP_HELPERS = CAP_HELPERS
    runner.REPETITIONS = repetitions
    runner.ORDER_SEED = seed
    runner.TIMEOUT_SECONDS = MAX_RUN_SECONDS
    runner.CC = CC
    return runner.make_plan()


def run_batch(output, phase, depth, threads, hashes, repetitions, deadline):
    number = len(read_csv(output / "batches.csv")) + 1
    batch = output / f"{number:02d}-{phase}-depth{depth}"
    (output / "status.txt").write_text(f"running {batch.name}\n")
    plan = configure(depth, threads, hashes, repetitions, ORDER_SEED + number)
    start = time.monotonic()
    state = "complete"
    imported = 0
    try:
        runner.execute(plan, batch, deadline=deadline)
    except (runner.BudgetExpired, subprocess.TimeoutExpired):
        state = "budget-limited" if time.monotonic() >= deadline else "timeout"
        print(f"{batch.name}: {state}; preserving logs and completed results.", flush=True)
    except BaseException:
        state = "failed/interrupted"
        raise
    finally:
        elapsed = time.monotonic() - start
        if phase != "pilot":
            imported = collect_balanced(batch, output, phase, plan)
        append_csv(output / "batches.csv", [{
            "batch": batch.name, "phase": phase, "depth": depth, "status": state,
            "planned_runs": len(plan), "completed_runs": len(read_csv(batch / "runs.csv")),
            "balanced_runs_imported": imported, "elapsed_seconds": round(elapsed, 3),
        }])
    return state == "complete", elapsed


def fits(pilot_seconds, repetitions, deadline):
    estimate = SAFETY_FACTOR * pilot_seconds * repetitions + RESERVE_SECONDS
    return estimate <= deadline - time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pilot-only", action="store_true", help="Only calibrate depth 15")
    parser.add_argument("--hours", type=float, default=HOURS)
    args = parser.parse_args()
    if not 0 < args.hours <= 24 or not DEPTHS or DEPTHS != sorted(set(DEPTHS)):
        raise ValueError("Use a positive budget up to 24 hours and increasing unique depths")
    if any(d < 15 or d > 100 for d in DEPTHS):
        raise ValueError("Depth sweep must be between 15 and 100")
    baseline = configure(15, BASELINE_THREADS, [64], BASELINE_REPETITIONS, ORDER_SEED)
    pilot = configure(DEPTHS[0], DEPTH_THREADS, HASH_SIZES_MB, 1, ORDER_SEED)
    configure(DEPTHS[-1], DEPTH_THREADS, HASH_SIZES_MB, DEPTH_REPETITIONS, ORDER_SEED)
    print(f"Budget: {args.hours:g} h. Baseline: {len(baseline)} complete benchmarks.")
    print(f"Depth sweep: {len(pilot)} configurations/depth, {DEPTH_REPETITIONS} measured repetitions.")
    print(f"Threads: baseline={BASELINE_THREADS}; depth sweep={DEPTH_THREADS}.")
    print(f"TT MiB={HASH_SIZES_MB}; cap_helpers={CAP_HELPERS}; buckets=2 entries.")
    print("One pilot repetition precedes each depth; pilots are excluded from combined CSVs.")
    print("The deepest complete feasible depth gets extra balanced repetitions if time remains.")
    if args.dry_run:
        return
    if hasattr(runner.os, "sched_getaffinity"):
        if max(BASELINE_THREADS + DEPTH_THREADS) > len(runner.os.sched_getaffinity(0)):
            raise ValueError("Requested threads exceed allowed CPUs; check your allocation")
    if runner.capture(["git", "ls-files", "--others", "--exclude-standard", "--", "src"], required=True):
        raise ValueError("Untracked files under src/: record intended source changes before running")

    started = time.monotonic()
    deadline = started + args.hours * 3600
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = runner.ROOT / "results" / "experiments" / f"overnight-{stamp}"
    output.mkdir(parents=True)
    shutil.copyfile(Path(__file__), output / "overnight.py")
    settings = {name: value for name, value in globals().items() if name.isupper()}
    append_csv(output / "settings.csv", [{"setting": key, "value": repr(value)}
                                        for key, value in settings.items()])
    append_csv(output / "settings.csv", [{"setting": "effective_hours", "value": args.hours}])
    status = output / "status.txt"
    status.write_text("running\n")
    print(f"Overnight results: {output}", flush=True)
    try:
        if args.pilot_only:
            ok, _ = run_batch(output, "pilot", 15, DEPTH_THREADS, HASH_SIZES_MB, 1, deadline)
            status.write_text("pilot-complete\n" if ok else "pilot-incomplete\n")
            return
        ok, _ = run_batch(output, "baseline", 15, BASELINE_THREADS, [64],
                          BASELINE_REPETITIONS, deadline)
        if not ok:
            status.write_text("baseline-incomplete; see batches.csv\n")
            return

        last_depth, last_pilot = None, None
        for depth in DEPTHS:
            # Avoid spending the reserved time probing another depth.
            if deadline - time.monotonic() <= RESERVE_SECONDS:
                break
            remaining = deadline - time.monotonic() - RESERVE_SECONDS
            pilot_deadline = time.monotonic() + remaining / (1 + SAFETY_FACTOR * DEPTH_REPETITIONS)
            ok, seconds = run_batch(output, "pilot", depth, DEPTH_THREADS,
                                    HASH_SIZES_MB, 1, pilot_deadline)
            if not ok or not fits(seconds, DEPTH_REPETITIONS, deadline):
                print(f"Depth {depth}: stopping the sweep; insufficient measured budget or timeout.", flush=True)
                break
            ok, _ = run_batch(output, "depth", depth, DEPTH_THREADS, HASH_SIZES_MB,
                              DEPTH_REPETITIONS, deadline)
            if not ok:
                break
            last_depth, last_pilot = depth, seconds

        while last_depth is not None and fits(last_pilot, 1, deadline):
            ok, _ = run_batch(output, "extra", last_depth, DEPTH_THREADS,
                              HASH_SIZES_MB, 1, deadline)
            if not ok:
                break
        status.write_text("finished; see batches.csv for depth coverage and timeouts\n")
    except BaseException:
        status.write_text("failed/interrupted; completed data retained\n")
        raise
    print(f"Saved balanced non-pilot data: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
