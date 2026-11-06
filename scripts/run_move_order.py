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
DEPTH = 15
THREAD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 192]  # Total workers, main included.
HASH_MB = 64
REPETITIONS = 5                    # Per thread count and move-order setting.
ORDER_SEED = 2026                  # Randomizes order within each repetition.
CC = "gcc"
TIME_BUDGET_HOURS = 6.5
TIMEOUT_SECONDS = 300              # Whole 50-position benchmark; None disables.
# ===============================================================================


def main():
    runner.CC = CC
    runner.ORDER_SEED = ORDER_SEED
    runner.TIME_BUDGET_HOURS = TIME_BUDGET_HOURS
    runner.TIMEOUT_SECONDS = TIMEOUT_SECONDS
    plan = runner.make_plan(
        buckets=[BUCKET_SIZE], depths=[DEPTH], threads=THREAD_COUNTS,
        hashes=[HASH_MB], caps=[0], rotations=[0, 1],
        repetitions=REPETITIONS, phase="move-order")
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
