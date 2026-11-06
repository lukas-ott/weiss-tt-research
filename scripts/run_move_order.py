#!/usr/bin/env python3
"""Compare normal helper root ordering with worker-index rotation.

This runner uses the same 50-position benchmark and writes its own results folder.
It starts no runs with --dry-run.
"""

from pathlib import Path
import subprocess
import sys

import run_experiments as runner

# =========================== EDIT MOVE-ORDER TEST HERE ==========================
BUCKET_SIZE = 2
DEPTHS = [15]
THREAD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 192]  # Total workers, main included.
HASH_SIZES_MB = [64]
REPETITIONS = 30                   # Baseline phase repetitions, per move-order mode.
ORDER_SEED = 2026
CC = "gcc"
TIME_BUDGET_HOURS = 6.5            # This includes both move-order modes.
TIMEOUT_SECONDS = 300              # Whole 50-position benchmark; None disables.

# Same depth/hash sweep as run_experiments.py. Both settings are per move-order mode.
SWEEP_DEPTHS = list(range(15, 24))
SWEEP_THREADS = [1, 32, 128, 192]
SWEEP_HASH_SIZES_MB = [2, 64, 512]
SWEEP_REPETITIONS = 5
# ===============================================================================


def main():
    runner.CC = CC
    runner.ORDER_SEED = ORDER_SEED
    runner.TIME_BUDGET_HOURS = TIME_BUDGET_HOURS
    runner.TIMEOUT_SECONDS = TIMEOUT_SECONDS
    plan = runner.make_plan(
        buckets=[BUCKET_SIZE], depths=DEPTHS, threads=THREAD_COUNTS,
        hashes=HASH_SIZES_MB, caps=[0], rotations=[0, 1],
        repetitions=REPETITIONS, phase="baseline")
    for depth in SWEEP_DEPTHS:
        plan.extend(runner.make_plan(
            buckets=[BUCKET_SIZE], depths=[depth], threads=SWEEP_THREADS,
            hashes=SWEEP_HASH_SIZES_MB, caps=[0], rotations=[0, 1],
            repetitions=SWEEP_REPETITIONS, phase="depth-sweep"))
    for number, run in enumerate(plan, 1):
        run["run"] = number
    runner.main(plan=plan, runner_file=Path(__file__), results_prefix="move-order-")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; completed results and raw logs were retained.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Move-order experiment stopped: {error}", file=sys.stderr)
        sys.exit(1)
