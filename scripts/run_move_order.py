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
DEPTHS = list(range(15, 24))       # Depths 15 through 23; range's end is exclusive.
THREAD_COUNTS = [1, 32, 128, 192]  # Total workers, main included.
HASH_SIZES_MB = [2, 64, 512]
REPETITIONS = 5                    # Paired repetitions per configuration.
ORDER_SEED = 2026
CC = "gcc"
TIME_BUDGET_HOURS = 6.5            # Includes both move-order modes and all grid settings.
TIMEOUT_SECONDS = 300              # Whole 50-position benchmark; None disables.
# ===============================================================================


def main():
    runner.CC = CC
    runner.ORDER_SEED = ORDER_SEED
    runner.TIME_BUDGET_HOURS = TIME_BUDGET_HOURS
    runner.TIMEOUT_SECONDS = TIMEOUT_SECONDS
    plan = runner.make_plan(
        buckets=[BUCKET_SIZE], depths=DEPTHS, threads=THREAD_COUNTS,
        hashes=HASH_SIZES_MB, caps=[0], rotations=[0, 1],
        repetitions=REPETITIONS, phase="move-order")
    for number, run in enumerate(plan, 1):
        run["run"] = number
    runner.main(
        plan=plan,
        runner_file=Path(__file__),
        experiment_slug="helper-root-move-order",
        experiment_record={
            "title": "Helper root move ordering",
            "question": "Does rotating each helper's ordered root move list by worker index change time to depth, search work or result stability?",
            "hypothesis": "Different helper root orders may diversify parallel search and reduce redundant work, though they may also delay useful shared-table information.",
            "controls": [
                "Normal order and rotated order are paired at every selected configuration and repetition.",
                "The same executable, benchmark positions, depth, thread count and TT size are used in each pair.",
                "Helpers remain uncapped in both modes.",
                "Runs are sequential; only helper root ordering changes between treatments.",
            ],
            "limitations": [
                "This is a benchmark-time and search-work test, not a playing-strength test.",
                "Results depend on the chosen grid, machine placement and source revision.",
                "Parallel search results can vary between repetitions even with a fixed plan order seed.",
            ],
        },
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; completed results and raw logs were retained.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Move-order experiment stopped: {error}", file=sys.stderr)
        sys.exit(1)
