#!/usr/bin/env python3
"""Summarize moves and non-mate scores across repetitions using pandas."""

from pathlib import Path
import pandas as pd

# SETTINGS: change this to ONE experiment folder containing positions.csv.
EXPERIMENT_DIR = Path("results/experiments/20261028T143204152830Z")


def summarize(positions):
    # Keep different positions, configurations and engine builds separate.
    group_columns = [
        "bucket_size", "depth", "threads", "hash_mb", "binary_sha256", "position"
    ]
    rows = []

    for values, group in positions.groupby(group_columns):
        row = dict(zip(group_columns, values))
        move_counts = group["bestmove"].value_counts()

        # Mate distances are not centipawn evaluations: exclude them from statistics.
        cp_scores = group.loc[group["score_type"] == "cp", "score"]

        row["observations"] = len(group)
        row["distinct_moves"] = len(move_counts)
        row["moves_returned"] = "; ".join(
            f"{move}: {count}" for move, count in move_counts.items()
        )
        row["cp_count"] = len(cp_scores)
        row["mate_count"] = (group["score_type"] == "mate").sum()
        row["cp_mean"] = cp_scores.mean()
        # Sample variance (divide by n-1), in centipawns squared.
        # Pandas leaves this blank in the CSV when fewer than two cp scores exist.
        row["cp_variance"] = cp_scores.var(ddof=1)
        row["cp_stddev"] = cp_scores.std(ddof=1)
        rows.append(row)

    return pd.DataFrame(rows)


def main():
    positions = pd.read_csv(EXPERIMENT_DIR / "positions.csv")
    summary = summarize(positions)
    output_file = EXPERIMENT_DIR / "position_summary.csv"
    summary.to_csv(output_file, index=False, float_format="%.4f")

    # Hide the long build hash when printing; keep it in the saved CSV.
    print(summary.drop(columns="binary_sha256").to_string(index=False))
    print(f"\nSaved: {output_file}")


if __name__ == "__main__":
    main()
