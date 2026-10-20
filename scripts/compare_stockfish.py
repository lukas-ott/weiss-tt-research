#!/usr/bin/env python3
"""Compare one Weiss experiment with the saved Stockfish top-five reference."""

from pathlib import Path
import re

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

# ============================= EDIT SETTINGS HERE =============================
EXPERIMENT_DIR = ROOT / "results/experiments/20261028_depth15-thread-scaling-kiloampere"
REFERENCE_DIR = ROOT / "results/stockfish19-reference-20261029"
OUTPUT_DIR = ROOT / "results/stockfish-comparison/kiloampere-20261028"
NEAR_BEST_CP = 25  # Descriptive tolerance, in Stockfish's own score scale.
# =============================================================================

CONFIG = ["condition", "bucket_size", "depth", "threads", "hash_mb", "cap_helpers", "rotate_root_moves", "cpu_binding", "binary_sha256"]


def compare(positions, reference):
    """One output row per Weiss search; missing candidate scores stay missing."""
    positions = positions.copy()
    if "cap_helpers" not in positions:
        positions["cap_helpers"] = 0
    if "rotate_root_moves" not in positions:
        positions["rotate_root_moves"] = 0
    if "cpu_binding" not in positions:
        positions["cpu_binding"] = "none"
    positions["cpu_binding"] = positions["cpu_binding"].fillna("none")
    if "condition" not in positions:
        positions["condition"] = "candidate"
    if reference.duplicated(["position", "move"]).any():
        raise ValueError("Duplicate reference position/move")
    for _, group in reference.groupby("position"):
        if sorted(group["rank"].tolist()) != [1, 2, 3, 4, 5]:
            raise ValueError("Expected exactly ranks 1..5 for each reference position")

    expected = set(reference["position"])
    for _, group in positions.groupby("run"):
        if (set(group["position"]) != expected or group["position"].duplicated().any()
                or len(group[CONFIG + ["repetition"]].drop_duplicates()) != 1):
            raise ValueError("Expected one complete position suite and configuration per run")
    if positions.empty or reference.empty:
        raise ValueError("Input CSV is empty")

    reference = reference.copy()
    reference["score_cp"] = reference["score_cp"].where(reference["score_type"] == "cp")
    best = reference.loc[reference["rank"] == 1,
                         ["position", "move", "score_cp", "score_type", "fen"]]
    best = best.rename(columns={"move": "sf_bestmove", "score_cp": "sf_best_cp",
                                "score_type": "sf_best_score_type"})
    candidates = reference[["position", "move", "rank", "score_cp", "score_type"]]
    candidates = candidates.rename(columns={"move": "bestmove", "rank": "sf_move_rank",
                                            "score_cp": "sf_chosen_move_cp",
                                            "score_type": "sf_chosen_move_score_type"})
    rows = positions.merge(best, on="position", how="left", validate="many_to_one")
    rows = rows.merge(candidates, on=["position", "bestmove"], how="left",
                      validate="many_to_one")

    weiss_cp = rows["score"].where(rows["score_type"] == "cp")
    # Both engines report from the side to move: no sign flip for Black.
    rows["score_difference_cp"] = weiss_cp - rows["sf_best_cp"]
    rows["absolute_score_difference_cp"] = rows["score_difference_cp"].abs()
    rows["chosen_score_difference_cp"] = weiss_cp - rows["sf_chosen_move_cp"]
    rows["absolute_chosen_score_difference_cp"] = rows["chosen_score_difference_cp"].abs()
    rows["best_move_agrees"] = rows["bestmove"] == rows["sf_bestmove"]
    rows["move_in_top5"] = rows["sf_move_rank"].notna()
    # This gap compares two Stockfish scores, avoiding mixed evaluation scales.
    rows["move_gap_cp"] = rows["sf_best_cp"] - rows["sf_chosen_move_cp"]
    rows["near_best"] = (rows["move_gap_cp"] <= NEAR_BEST_CP).astype(float)
    rows["near_best"] = rows["near_best"].where(rows["move_gap_cp"].notna())
    return rows


def summarize(rows, groups):
    """Means use available values; explicit counts expose every denominator."""
    return rows.groupby(groups).agg(
        observations=("position", "size"),
        cp_comparisons=("score_difference_cp", "count"),
        mean_absolute_score_difference_cp=("absolute_score_difference_cp", "mean"),
        median_absolute_score_difference_cp=("absolute_score_difference_cp", "median"),
        mean_signed_score_difference_cp=("score_difference_cp", "mean"),
        best_move_agreement_pct=("best_move_agrees", lambda s: 100 * s.mean()),
        top5_coverage_pct=("move_in_top5", lambda s: 100 * s.mean()),
        scored_moves=("move_gap_cp", "count"),
        mean_move_gap_cp=("move_gap_cp", "mean"),
        near_best_pct_of_scored_moves=("near_best", lambda s: 100 * s.mean()),
        chosen_cp_comparisons=("chosen_score_difference_cp", "count"),
        mean_absolute_chosen_score_difference_cp=("absolute_chosen_score_difference_cp", "mean"),
    ).reset_index().assign(near_best_threshold_cp=NEAR_BEST_CP)


def main():
    positions = pd.read_csv(EXPERIMENT_DIR / "positions.csv")
    reference = pd.read_csv(REFERENCE_DIR / "top5.csv")
    # Position IDs are only safe join keys while they refer to the same FEN suite.
    source = (ROOT / "src/tests.c").read_text()
    fen_array = re.search(r"BenchmarkFENs\[\]\s*=\s*\{(.*?)\};", source, re.S)
    if fen_array is None:
        raise ValueError("Cannot find BenchmarkFENs in src/tests.c")
    expected_fens = re.findall(r'"([^"]+)"', fen_array.group(1))
    saved_fens = pd.read_csv(REFERENCE_DIR / "positions.csv").sort_values("position")
    if saved_fens["position"].tolist() != list(range(1, len(expected_fens) + 1)):
        raise ValueError("Reference position IDs do not match src/tests.c")
    if saved_fens["fen"].tolist() != expected_fens:
        raise ValueError("Reference FENs do not match src/tests.c")
    if not reference["fen"].eq(reference["position"].map(saved_fens.set_index("position")["fen"])).all():
        raise ValueError("top5.csv and positions.csv have different FENs")

    if "cap_helpers" not in positions:
        positions["cap_helpers"] = 0
    if "rotate_root_moves" not in positions:
        positions["rotate_root_moves"] = 0
    if "cpu_binding" not in positions:
        positions["cpu_binding"] = "none"
    positions["cpu_binding"] = positions["cpu_binding"].fillna("none")
    if "condition" not in positions:
        positions["condition"] = "candidate"
    rows = compare(positions, reference)
    repetitions = summarize(rows, CONFIG + ["run", "repetition"])
    timings = rows.groupby("run").agg(wall_ns=("wall_ns", "sum")) / 1e9
    timings = timings.rename(columns={"wall_ns": "wall_seconds"})
    repetitions = repetitions.merge(timings, on="run", validate="one_to_one")

    configurations = summarize(rows, CONFIG)
    spread = repetitions.groupby(CONFIG).agg(
        repetitions=("run", "size"),
        mean_wall_seconds=("wall_seconds", "mean"),
        stddev_wall_seconds=("wall_seconds", "std"),
        stddev_repetition_mae_cp=("mean_absolute_score_difference_cp", "std"),
        stddev_repetition_agreement_percentage_points=("best_move_agreement_pct", "std"),
    ).reset_index()
    configurations = configurations.merge(spread, on=CONFIG, validate="one_to_one")

    per_position = summarize(rows, CONFIG + ["position"])
    variability = rows.assign(cp_score=rows["score"].where(rows["score_type"] == "cp"))
    variability = variability.groupby(CONFIG + ["position"]).agg(
        distinct_moves=("bestmove", "nunique"),
        mean_weiss_cp=("cp_score", "mean"),
        stddev_weiss_cp=("cp_score", "std"),
    ).reset_index()
    per_position = per_position.merge(variability, on=CONFIG + ["position"], validate="one_to_one")

    # Same fully scored positions across ALL configurations: avoids changing coverage.
    covered = rows.groupby("position")["move_gap_cp"].agg(lambda s: s.notna().all())
    common = rows.loc[rows["position"].isin(covered.index[covered])]
    missing = rows.loc[~rows["move_in_top5"]].groupby(CONFIG + ["position", "bestmove"])
    missing = missing.size().rename("observations").reset_index()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, table in {
        "observations": rows, "configurations": configurations,
        "repetitions": repetitions, "positions": per_position,
        "common_positions": summarize(common, CONFIG), "unscored_moves": missing,
    }.items():
        table.to_csv(OUTPUT_DIR / f"{name}.csv", index=False, float_format="%.4f")

    print(configurations[["bucket_size", "depth", "threads", "hash_mb", "cap_helpers", "mean_wall_seconds",
                          "mean_absolute_score_difference_cp", "best_move_agreement_pct",
                          "top5_coverage_pct", "mean_move_gap_cp"]]
          .to_string(index=False))
    print(f"\nCommon fully scored positions: {int(covered.sum())}/{len(covered)}")
    print(f"Saved comparisons to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
