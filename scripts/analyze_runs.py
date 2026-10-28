#!/usr/bin/env python3
"""Summarize benchmark runtimes across repetitions using pandas."""

from pathlib import Path
import pandas as pd

# SETTINGS: change this to ONE experiment folder containing runs.csv.
EXPERIMENT_DIR = Path("results/experiments/20261028T143204152830Z")


def summarize(runs):
    # Keep configurations and engine builds separate.
    group_columns = [
        "bucket_size", "depth", "threads", "hash_mb", "binary_sha256"
    ]
    rows = []

    for values, group in runs.groupby(group_columns):
        row = dict(zip(group_columns, values))
        runtime_s = group["wall_ns"] / 1_000_000_000
        cpu_s = group["cpu_ns"] / 1_000_000_000

        row["repetitions"] = len(group)
        row["runtime_mean_s"] = runtime_s.mean()
        row["runtime_median_s"] = runtime_s.median()
        row["runtime_min_s"] = runtime_s.min()
        row["runtime_max_s"] = runtime_s.max()
        # Sample variance (seconds squared) and standard deviation (seconds).
        # With fewer than two repetitions, pandas leaves these blank in the CSV.
        row["runtime_variance_s2"] = runtime_s.var(ddof=1)
        row["runtime_stddev_s"] = runtime_s.std(ddof=1)
        # CPU time is summed across workers; wall time measures time to depth.
        row["cpu_mean_s"] = cpu_s.mean()
        row["nodes_mean"] = group["nodes"].mean()
        row["main_nodes_mean"] = group["main_nodes"].mean()
        row["throughput_mean_nodes_s"] = (group["nodes"] / runtime_s).mean()
        rows.append(row)

    return pd.DataFrame(rows)


def main():
    runs = pd.read_csv(EXPERIMENT_DIR / "runs.csv")
    summary = summarize(runs)
    output_file = EXPERIMENT_DIR / "run_summary.csv"
    summary.to_csv(output_file, index=False, float_format="%.4f")

    # Hide the long build hash when printing; keep it in the saved CSV.
    print(summary.drop(columns="binary_sha256").to_string(index=False))
    print(f"\nSaved: {output_file}")


if __name__ == "__main__":
    main()
