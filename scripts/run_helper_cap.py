#!/usr/bin/env python3
"""One-off comparison of the original and depth-capped helper policies."""

from pathlib import Path
import subprocess
import sys

import run_experiments as runner

# ========================== EDIT CAP TEST SETTINGS HERE ========================
BUCKET_SIZE = 2
DEPTH = 15
THREAD_COUNTS = [1, 32, 128]        # Total workers, including the main worker.
HASH_MB = 64
REPETITIONS = 5                    # Per thread count and per capped/uncapped variant.
ORDER_SEED = 2026
CC = "gcc"
TIME_BUDGET_HOURS = 6.5
TIMEOUT_SECONDS = 300              # Entire 50-position benchmark; None disables.
# ==============================================================================


def main():
    # Reuse building, logging and validation without scheduling the normal study.
    runner.CC = CC
    runner.ORDER_SEED = ORDER_SEED
    runner.TIME_BUDGET_HOURS = TIME_BUDGET_HOURS
    runner.TIMEOUT_SECONDS = TIMEOUT_SECONDS
    plan = runner.make_plan(buckets=[BUCKET_SIZE], depths=[DEPTH],
                            threads=THREAD_COUNTS, hashes=[HASH_MB], caps=[0, 1],
                            repetitions=REPETITIONS, phase="helper-cap")
    runner.main(
        plan=plan,
        runner_file=Path(__file__),
        experiment_slug="helper-depth-cap",
        experiment_record={
            "title": "Helper search depth cap",
            "question": "Does preventing helpers from searching beyond the requested benchmark depth change time to depth or aggregate search work?",
            "hypothesis": "A helper depth cap may reduce wasted deeper work but can also remove useful parallel search information; the direction of the wall-time change is empirical.",
            "controls": [
                "Capped and uncapped runs use the same engine source and benchmark positions.",
                "The only intended treatment difference is BENCH_CAP_HELPERS.",
                "The plan pairs both cap modes across the same thread counts and repetitions.",
                "Runs are sequential and inherit the same machine placement policy.",
            ],
            "limitations": [
                "This is a benchmark-time comparison, not a playing-strength test.",
                "At one worker, the helper cap has no effect because there are no helpers.",
                "Results apply to the requested depth, TT size, engine revision and machine used.",
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
        print(f"Cap test stopped: {error}", file=sys.stderr)
        sys.exit(1)
