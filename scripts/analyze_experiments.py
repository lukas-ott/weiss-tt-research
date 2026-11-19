#!/usr/bin/env python3
"""Summarize Weiss experiment CSVs and generate a local HTML/PNG analysis suite.

Example:
    python3 scripts/analyze_experiments.py results/experiments/20261028_depth15-thread-scaling-macos
    python3 scripts/analyze_experiments.py results/experiments/run-a results/experiments/run-b \
        --reference results/stockfish19-reference-20261029/top5.csv

Lines show configuration means across completed repetitions. Error bars show
sample standard deviation across repetitions (or positions for stability plots).
No engine searches are started.
"""

import argparse
import html
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = ROOT / "results/stockfish19-reference-20261029/top5.csv"
NEAR_BEST_CP = 25
PHASE_TIMINGS = ["helper_launch_ns", "time_to_depth_ns", "helper_stop_to_join_ns"]
CONFIG = ["experiment", "phase", "condition", "bucket_size", "depth", "threads", "hash_mb", "cap_helpers", "rotate_root_moves", "cpu_binding", "applied_cpu_binding", "binary_sha256"]
NUMERIC_RUN = ["run", "repetition", "bucket_size", "depth", "threads", "hash_mb",
               "cap_helpers", "rotate_root_moves", "wall_ns", "cpu_ns", "main_nodes", "nodes"]
NUMERIC_POSITION = ["run", "repetition", "bucket_size", "depth", "threads", "hash_mb",
                    "cap_helpers", "rotate_root_moves", "position", "wall_ns", "cpu_ns", "main_nodes", "nodes", "score"]

# ============================== EDIT SETTINGS HERE =============================
# Put experiment folders here to run the script without command-line arguments.
# Example: [ROOT / "results/experiments/20261028_depth15-thread-scaling-macos"]
EXPERIMENT_DIRS = []
REFERENCE_CSV = DEFAULT_REFERENCE  # Set to None to skip Stockfish comparisons.
OUTPUT_DIR = None                 # None writes to analysis/ inside the first experiment.

# Keep all values (None means include every available value).
FILTERS = {
    "condition": None,            # Example: ["candidate"]
    "bucket_size": None,          # Example: [2]
    "depth": None,                # Example: [15, 20, 23]
    "threads": None,              # Example: [1, 4, 8, 32, 128, 192]
    "hash_mb": None,              # Example: [2, 64, 512]
    "cap_helpers": None,          # Example: [0] for uncapped, [1] for capped
    "rotate_root_moves": None,    # Example: [0] original, [1] worker-index rotation
    "cpu_binding": None,          # Example: ["none"] or ["physical"] on Linux
}

CHART_GROUPS = {
    "scaling": True, "across": True, "work": True, "phases": True,
    "tt": True, "comparisons": True, "stability": True, "stockfish": True,
}
SHOW_ERROR_BARS = True            # ±1 sample standard deviation
FIGURE_DPI = 125

# Reference value for comparisons along each parameter. Use None to disable.
COMPARISON_REFERENCES = {
    "threads": 1, "bucket_size": 2, "hash_mb": 64, "depth": None, "cap_helpers": 0,
    "rotate_root_moves": 0, "cpu_binding": "none", "condition": "baseline",
}
# ===============================================================================


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiments", nargs="*", type=Path,
                        help="Experiment folders; command-line values override EXPERIMENT_DIRS above")
    parser.add_argument("--reference", type=Path,
                        help="Stockfish top5.csv; overrides REFERENCE_CSV above")
    parser.add_argument("--no-reference", action="store_true",
                        help="Make performance/stability charts without Stockfish comparisons")
    parser.add_argument("--output", type=Path,
                        help="Output folder; overrides OUTPUT_DIR above")
    args = parser.parse_args()
    configured_paths = args.experiments or EXPERIMENT_DIRS
    if not configured_paths:
        parser.error("Set EXPERIMENT_DIRS at the top of this file or pass an experiment folder")
    args.experiments = configured_paths
    if args.reference is None:
        args.reference = REFERENCE_CSV
    if args.output is None:
        args.output = OUTPUT_DIR
    return args


def numeric_columns(frame, columns):
    for column in columns:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def read_experiments(paths):
    run_frames, position_frames = [], []
    for source in paths:
        source = source.expanduser().resolve()
        runs_file, positions_file = source / "runs.csv", source / "positions.csv"
        if not runs_file.is_file() or not positions_file.is_file():
            raise FileNotFoundError(f"Expected both runs.csv and positions.csv in {source}")
        runs = pd.read_csv(runs_file)
        positions = pd.read_csv(positions_file)
        for label, frame in (("runs.csv", runs), ("positions.csv", positions)):
            required = {"bucket_size", "depth", "threads", "hash_mb", "run", "repetition"}
            required |= {"binary_sha256"} if "binary_sha256" in frame else set()
            required |= ({"wall_ns", "cpu_ns", "main_nodes", "nodes"} if label == "runs.csv"
                         else {"position", "wall_ns", "cpu_ns", "main_nodes", "nodes", "bestmove", "score_type", "score"})
            missing = required - set(frame.columns)
            if missing:
                raise ValueError(f"{source / label} is missing columns: {', '.join(sorted(missing))}")
            if "cap_helpers" not in frame:
                frame["cap_helpers"] = 0
            if "rotate_root_moves" not in frame:
                frame["rotate_root_moves"] = 0
            if "cpu_binding" not in frame:
                frame["cpu_binding"] = "none"
            frame["cpu_binding"] = frame["cpu_binding"].fillna("none")
            if "condition" not in frame:
                frame["condition"] = "candidate"
            numeric_columns(frame, ["rotate_root_moves"])
            if "binary_sha256" not in frame:
                frame["binary_sha256"] = "unknown"
            frame["binary_sha256"] = frame["binary_sha256"].fillna("unknown").astype(str)
            numeric_columns(frame, NUMERIC_RUN if label == "runs.csv" else NUMERIC_POSITION)
            if "phase" not in frame:
                frame["phase"] = "unspecified"
            if "applied_cpu_binding" not in frame:
                if label == "positions.csv" and "applied_cpu_binding" in runs:
                    frame["applied_cpu_binding"] = frame["run"].map(
                        runs.set_index("run")["applied_cpu_binding"])
                else:
                    frame["applied_cpu_binding"] = "unknown"
            frame["phase"] = frame["phase"].replace({"default": "regular", "extra": "regular"})
            frame["applied_cpu_binding"] = frame["applied_cpu_binding"].fillna("unknown")
            frame["experiment"] = str(source)
            if frame[CONFIG + ["run", "repetition"]].isna().any().any():
                raise ValueError(f"Missing configuration or run identity in {source / label}")
            frame["run_key"] = source.as_posix() + "::" + frame["run"].astype(int).astype(str)
        run_frames.append(runs)
        position_frames.append(positions)

    # Old logs have no phase timings. Leave them unmeasured after concatenation.
    runs = pd.concat(run_frames, ignore_index=True)
    positions = pd.concat(position_frames, ignore_index=True)
    if runs.empty or positions.empty:
        raise ValueError("Experiment CSVs must contain at least one complete run")
    if runs["run_key"].duplicated().any():
        raise ValueError("A run ID occurs more than once within an experiment folder")
    if positions.duplicated(["run_key", "position"]).any():
        raise ValueError("A run contains duplicate position rows")
    position_ids = positions.groupby("run_key")["position"].agg(set)
    expected_positions = set(range(1, 51))
    if set(runs["run_key"]) != set(position_ids.index) or any(
            ids != expected_positions for ids in position_ids):
        raise ValueError("Expected positions 1..50 for every completed run")
    identity = ["run_key", "repetition", *CONFIG]
    if positions[identity].drop_duplicates().merge(
            runs[identity], how="outer", indicator=True)["_merge"].ne("both").any():
        raise ValueError("Position and run CSVs disagree on configuration identity")
    return runs, positions


def apply_filters(runs, positions):
    for column, allowed in FILTERS.items():
        if allowed is None:
            continue
        if not isinstance(allowed, (list, tuple, set)):
            raise ValueError(f"FILTERS[{column!r}] must be a list/tuple/set or None")
        runs = runs.loc[runs[column].isin(allowed)].copy()
        positions = positions.loc[positions[column].isin(allowed)].copy()
    keep_runs = set(runs["run_key"])
    positions = positions.loc[positions["run_key"].isin(keep_runs)].copy()
    if runs.empty or positions.empty:
        raise ValueError("No complete experiment data remains after applying FILTERS")
    return runs, positions


def load_reference(path):
    reference = pd.read_csv(path)
    needed = {"position", "rank", "move", "score_type", "score_cp", "fen"}
    if not needed.issubset(reference.columns):
        raise ValueError(f"Reference CSV must contain: {', '.join(sorted(needed))}")
    numeric_columns(reference, ["position", "rank", "score_cp"])
    reference["move"] = reference["move"].astype(str)

    # Position IDs only make a valid join when the FEN suite still matches.
    source_file = ROOT / "src/tests.c"
    if source_file.is_file():
        source = source_file.read_text(encoding="utf-8")
        match = re.search(r"BenchmarkFENs\[\]\s*=\s*\{(.*?)\};", source, re.S)
        if match:
            fens = re.findall(r'"([^"]+)"', match.group(1))
            positions = reference[["position", "fen"]].drop_duplicates().sort_values("position")
            if positions["position"].astype(int).tolist() != list(range(1, len(fens) + 1)):
                raise ValueError("Stockfish reference position IDs do not match src/tests.c")
            if positions["fen"].tolist() != fens:
                raise ValueError("Stockfish reference FENs differ from src/tests.c; refusing an unsafe position join")
    return reference


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
    for field, default in (("experiment", ""), ("phase", "unspecified"),
                           ("applied_cpu_binding", "unknown")):
        if field not in positions:
            positions[field] = default
    if reference.duplicated(["position", "move"]).any():
        raise ValueError("Duplicate reference position/move")
    for _, group in reference.groupby("position"):
        if sorted(group["rank"].tolist()) != [1, 2, 3, 4, 5]:
            raise ValueError("Expected exactly ranks 1..5 for each reference position")

    expected = set(reference["position"])
    for _, group in positions.groupby("run_key" if "run_key" in positions else "run"):
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
    rows["move_in_top3"] = rows["sf_move_rank"].le(3).fillna(False)
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


def make_run_summary(runs):
    data = runs.copy()
    data["wall_s"] = data["wall_ns"] / 1e9
    data["cpu_s"] = data["cpu_ns"] / 1e9
    data["nodes_per_s"] = data["nodes"] / data["wall_s"]
    data["effective_workers"] = data["cpu_ns"] / data["wall_ns"]
    data["helper_nodes"] = data["nodes"] - data["main_nodes"]
    aggregations = dict(
        repetitions=("run_key", "nunique"),
        wall_mean_s=("wall_s", "mean"), wall_sd_s=("wall_s", "std"),
        cpu_mean_s=("cpu_s", "mean"), cpu_sd_s=("cpu_s", "std"),
        nodes_mean=("nodes", "mean"), nodes_sd=("nodes", "std"),
        main_nodes_mean=("main_nodes", "mean"), main_nodes_sd=("main_nodes", "std"),
        nodes_per_s_mean=("nodes_per_s", "mean"),
        effective_workers_mean=("effective_workers", "mean"),
        helper_nodes_mean=("helper_nodes", "mean"),
    )
    if "process_wall_ns" in data:
        data["process_wall_s"] = data["process_wall_ns"] / 1e9
        aggregations["process_wall_mean_s"] = ("process_wall_s", "mean")
    if "helper_nodes_beyond_depth" in data:
        aggregations["helper_over_depth_nodes_mean"] = ("helper_nodes_beyond_depth", "mean")
    if set(PHASE_TIMINGS) <= set(data):
        data["launch"] = data["helper_launch_ns"] / 1e9
        data["depth_time"] = data["time_to_depth_ns"] / 1e9
        data["join"] = data["helper_stop_to_join_ns"] / 1e9
        data["after_depth"] = (data["wall_ns"] - data["time_to_depth_ns"]) / 1e9
        for label, column in [("launch", "launch"), ("depth", "depth_time"),
                              ("join", "join"), ("after_depth", "after_depth")]:
            aggregations[f"{label}_mean_s"] = (column, "mean")
            aggregations[f"{label}_sd_s"] = (column, "std")
        for label in ("launch", "join"):
            data[f"{label}_share"] = 100 * data[label] / data["wall_s"]
            aggregations[f"{label}_share_mean_pct"] = (f"{label}_share", "mean")
        aggregations["timed_repetitions"] = ("time_to_depth_ns", "count")
    summary = data.groupby(CONFIG, dropna=False).agg(**aggregations).reset_index()
    pairing = [column for column in CONFIG if column != "threads"]
    paired = summary.loc[summary["threads"] == 1, pairing + ["wall_mean_s"]]
    paired = paired.rename(columns={"wall_mean_s": "one_thread_wall_mean_s"})
    summary = summary.merge(paired, on=pairing, how="left", validate="many_to_one")
    summary["speedup_vs_1_thread"] = summary["one_thread_wall_mean_s"] / summary["wall_mean_s"]
    summary["scaling_efficiency_pct"] = 100 * summary["speedup_vs_1_thread"] / summary["threads"]
    return summary


def make_position_summary(positions, compared=None):
    rows = []
    for keys, group in positions.groupby(CONFIG + ["position"], dropna=False, sort=False):
        row = dict(zip(CONFIG + ["position"], keys))
        cp = group["score"].where(group["score_type"] == "cp").dropna()
        moves = group["bestmove"].astype(str).value_counts()
        row.update(
            observations=len(group), wall_mean_ms=group["wall_ns"].mean() / 1e6,
            wall_sd_ms=group["wall_ns"].std() / 1e6, nodes_mean=group["nodes"].mean(),
            main_nodes_mean=group["main_nodes"].mean(), cp_observations=len(cp),
            weiss_cp_mean=cp.mean(), weiss_cp_sd=cp.std(), weiss_cp_variance=cp.var(),
            mate_observations=int((group["score_type"] == "mate").sum()),
            distinct_moves=len(moves), modal_move_pct=100 * moves.iloc[0] / len(group),
            moves_returned="; ".join(f"{move} ({count})" for move, count in moves.items()),
        )
        for field in PHASE_TIMINGS:
            if field in group:
                row[field.replace("_ns", "_mean_ms")] = group[field].mean() / 1e6
        rows.append(row)
    result = pd.DataFrame(rows)
    if compared is not None:
        quality = []
        for keys, group in compared.groupby(CONFIG + ["position"], dropna=False, sort=False):
            row = dict(zip(CONFIG + ["position"], keys))
            row.update(
                top1_agreement_pct=100 * group["best_move_agrees"].mean(),
                top3_coverage_pct=100 * group["move_in_top3"].mean(),
                top5_coverage_pct=100 * group["move_in_top5"].mean(),
                stockfish_move_gap_cp_mean=group["move_gap_cp"].mean(),
                absolute_score_gap_cp_mean=group["absolute_score_difference_cp"].mean(),
                score_gap_cp_count=int(group["score_difference_cp"].notna().sum()),
                stockfish_ranked_move_count=int(group["sf_move_rank"].notna().sum()),
            )
            quality.append(row)
        result = result.merge(pd.DataFrame(quality), on=CONFIG + ["position"], how="left",
                              validate="one_to_one")
    return result


def make_stockfish_summary(compared):
    # First average the 50 positions within each independent benchmark repetition.
    per_run = compared.groupby(["run_key"] + CONFIG, dropna=False).agg(
        top1_agreement_pct=("best_move_agrees", lambda values: 100 * values.mean()),
        top3_coverage_pct=("move_in_top3", lambda values: 100 * values.mean()),
        top5_coverage_pct=("move_in_top5", lambda values: 100 * values.mean()),
        mean_absolute_score_gap_cp=("absolute_score_difference_cp", "mean"),
        mean_signed_score_gap_cp=("score_difference_cp", "mean"),
        mean_stockfish_move_gap_cp=("move_gap_cp", "mean"),
        cp_comparisons=("score_difference_cp", "count"),
        stockfish_scored_chosen_moves=("move_gap_cp", "count"),
    ).reset_index()
    summary = per_run.groupby(CONFIG, dropna=False).agg(
        repetitions=("run_key", "nunique"),
        top1_mean_pct=("top1_agreement_pct", "mean"), top1_sd_pct=("top1_agreement_pct", "std"),
        top3_mean_pct=("top3_coverage_pct", "mean"), top3_sd_pct=("top3_coverage_pct", "std"),
        top5_mean_pct=("top5_coverage_pct", "mean"), top5_sd_pct=("top5_coverage_pct", "std"),
        absolute_score_gap_mean_cp=("mean_absolute_score_gap_cp", "mean"),
        absolute_score_gap_sd_cp=("mean_absolute_score_gap_cp", "std"),
        signed_score_gap_mean_cp=("mean_signed_score_gap_cp", "mean"),
        signed_score_gap_sd_cp=("mean_signed_score_gap_cp", "std"),
        stockfish_move_gap_mean_cp=("mean_stockfish_move_gap_cp", "mean"),
        stockfish_move_gap_sd_cp=("mean_stockfish_move_gap_cp", "std"),
        cp_comparisons=("cp_comparisons", "sum"),
        stockfish_scored_chosen_moves=("stockfish_scored_chosen_moves", "sum"),
    ).reset_index()
    return summary


def make_stability_summary(position_summary):
    return position_summary.groupby(CONFIG, dropna=False).agg(
        positions=("position", "nunique"),
        cp_repeatable_positions=("cp_observations", lambda values: int((values >= 2).sum())),
        score_sd_mean_cp=("weiss_cp_sd", "mean"), score_sd_between_positions_cp=("weiss_cp_sd", "std"),
        distinct_moves_mean=("distinct_moves", "mean"), distinct_moves_sd=("distinct_moves", "std"),
        modal_move_mean_pct=("modal_move_pct", "mean"), modal_move_sd_pct=("modal_move_pct", "std"),
        positions_with_multiple_moves=("distinct_moves", lambda values: int((values > 1).sum())),
    ).reset_index()


def save_csv(frame, path):
    frame.to_csv(path, index=False, float_format="%.4f")


def matched_comparisons(runs, positions):
    """Match completed runs within an experiment and repetition, changing one setting."""
    metrics = ["wall_ns", "cpu_ns", "nodes", "main_nodes"]
    metrics += [field for field in PHASE_TIMINGS if field in runs]
    pairs, details = [], []
    position_metrics = positions[["run_key", "position", "bestmove", "score", "score_type",
                                  "wall_ns", "main_nodes"]]
    for setting, control in COMPARISON_REFERENCES.items():
        if control is None:
            continue
        keys = ["repetition"] + [column for column in CONFIG if column != setting]
        # Bucket layout and helper cap are compile-time settings. For binding and source builds,
        # report actual placement separately because those comparisons can change it.
        ignored = {
            "bucket_size": ["binary_sha256"],
            "cap_helpers": ["binary_sha256"],
            "cpu_binding": ["applied_cpu_binding"],
            "condition": ["binary_sha256", "applied_cpu_binding"],
        }.get(setting, [])
        keys = [column for column in keys if column not in ignored]
        group_keys = keys + [setting]
        if runs.duplicated(group_keys).any():
            # Ambiguous pairs are excluded rather than matched across builds.
            counts = runs.groupby(group_keys, dropna=False)["run_key"].transform("size")
            eligible = runs.loc[counts == 1]
        else:
            eligible = runs
        fields = list(dict.fromkeys(group_keys + ["run_key", *ignored, *metrics]))
        base = eligible.loc[eligible[setting] == control, fields].drop(columns=setting)
        other = eligible.loc[eligible[setting] != control, fields]
        matched = other.merge(base, on=keys, suffixes=("_variant", "_control"),
                              validate="many_to_one")
        if matched.empty:
            continue
        matched["setting"] = setting
        matched["variant"] = matched.pop(setting).astype(str)
        matched["control"] = str(control)
        for field in metrics:
            matched[field + "_change_pct"] = 100 * (
                matched[field + "_variant"] / matched[field + "_control"].replace(0, np.nan) - 1)
        position_pairs = matched[["run_key_variant", "run_key_control"]].merge(
            position_metrics, left_on="run_key_variant", right_on="run_key", validate="many_to_many"
        ).drop(columns="run_key").merge(
            position_metrics, left_on=["run_key_control", "position"],
            right_on=["run_key", "position"], suffixes=("_variant", "_control"),
            validate="many_to_one")
        position_pairs["same_move"] = 100.0 * position_pairs["bestmove_variant"].eq(
            position_pairs["bestmove_control"])
        both_cp = position_pairs["score_type_variant"].eq("cp") & position_pairs["score_type_control"].eq("cp")
        position_pairs["abs_cp_change"] = (
            position_pairs["score_variant"] - position_pairs["score_control"]).abs().where(both_cp)
        quality = position_pairs.groupby(["run_key_variant", "run_key_control"]).agg(
            move_agreement_pct=("same_move", "mean"),
            mean_abs_cp_change=("abs_cp_change", "mean"),
            cp_positions=("abs_cp_change", "count"),
        ).reset_index()
        position_pairs["wall_change_ms"] = (
            position_pairs["wall_ns_variant"] - position_pairs["wall_ns_control"]) / 1e6
        position_pairs["main_nodes_change"] = (
            position_pairs["main_nodes_variant"] - position_pairs["main_nodes_control"])
        details.append(position_pairs.drop(columns="run_key").assign(setting=setting))
        pairs.append(matched.merge(quality, on=["run_key_variant", "run_key_control"],
                                   validate="one_to_one"))
    if not pairs:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    # Aggregate each setting separately. It has a different set of fixed controls.
    summaries = []
    for pair_frame in pairs:
        setting = pair_frame["setting"].iloc[0]
        fixed = [column for column in CONFIG if column != setting and column in pair_frame]
        provenance = [column for column in pair_frame if column.startswith(
            ("binary_sha256_", "applied_cpu_binding_"))]
        columns = ["setting", "control", "variant", *fixed, *provenance]
        aggregations = {"pairs": ("repetition", "size")}
        for metric in [*(field + "_change_pct" for field in metrics),
                       "move_agreement_pct", "mean_abs_cp_change"]:
            aggregations[metric + "_mean"] = (metric, "mean")
            aggregations[metric + "_sd"] = (metric, "std")
            aggregations[metric + "_n"] = (metric, "count")
        summaries.append(pair_frame.groupby(columns, dropna=False).agg(**aggregations).reset_index())
    return (pd.concat(pairs, ignore_index=True), pd.concat(summaries, ignore_index=True),
            pd.concat(details, ignore_index=True))


def tt_diagnostics(positions, groups):
    """Pool counters before dividing. Empty denominators stay unmeasured."""
    if "tt_main_hit" not in positions:
        return pd.DataFrame()
    rows = positions.dropna(subset=["tt_main_hit"])
    if rows.empty:
        return pd.DataFrame()
    fields = [name for name in rows if name.startswith("tt_")]
    counts = rows.groupby(groups, dropna=False)[fields].sum(min_count=1).reset_index()
    bins = [name for name in fields if name.startswith("tt_buckets_")]
    bucket_count = counts[bins].sum(axis=1)
    occupied = sum(int(name.rsplit("_", 1)[1]) * counts[name].fillna(0) for name in bins)
    occupancy_pct = 100 * occupied / (counts["bucket_size"] * bucket_count)
    frames = []
    for role in ("main", "helpers"):
        prefix = f"tt_{role}_"
        raw = counts.filter(like=prefix).rename(columns=lambda name: name[len(prefix):])
        data = counts[groups + bins].copy()
        data["workers"] = role
        data["occupied_entries_pct"] = occupancy_pct
        probes = raw["ab_probes"] + raw["qs_probes"]
        for metric in ("hit", "empty_miss", "full_miss", "store_replace"):
            data[metric + "_per_1000_probes"] = 1000 * raw[metric] / probes.where(probes > 0)
        for numerator, denominator, label in [
                ("ab_cutoffs", "ab_probes", "ab_cutoff_pct"),
                ("qs_cutoffs", "qs_probes", "qs_cutoff_pct"),
                ("first_tt_move_cutoff", "first_tt_move", "first_tt_move_cutoff_pct"),
                ("changed_slots", "sampled_stores", "changed_slot_pct")]:
            data[label] = 100 * raw[numerator] / raw[denominator].where(raw[denominator] > 0)
        deep_victims = raw["replaced_depth_8_15"] + raw["replaced_depth_16_plus"]
        data["deep_replacements_per_1000_probes"] = 1000 * deep_victims / probes.where(probes > 0)
        data["mean_replaced_depth"] = raw["replaced_depth_sum"] / raw["store_replace"].where(raw["store_replace"] > 0)
        frames.append(pd.concat([data, raw], axis=1))
    return pd.concat(frames, ignore_index=True)


QUESTIONS = {
    "tt": ("What happens to entries during search?",
           "Compare full-bucket misses, usable bounds and sampled slot changes by bucket width. "
           "Main and helper measurements are reported separately."),
    "scaling": ("Where does scaling flatten?",
                "Compare wall time and aggregate throughput at fixed depth and TT settings. "
                "Speedup uses each batch’s own matching one-worker run."),
    "across": ("Does the scaling pattern repeat across batches?",
               "Compare speedup against each batch’s own one-worker result at the same recorded settings. "
               "Each line keeps its own phase and binary. Older runs may have unknown applied binding."),
    "work": ("Is extra search work buying time?",
             "Main-worker nodes measure the work on the search’s critical path. Helper nodes count "
             "work volume. The over-depth counter only covers work beyond the nominal target."),
    "phases": ("Where does the elapsed time go?",
               "Launch includes time during which early helpers may already be searching. "
               "The segments partition the benchmark clock for runs with all three measurements."),
    "comparisons": ("Which design change helped?",
                    "Each point averages matched repetition differences within one experiment. "
                    "Negative changes mean less time or fewer nodes. Bars show sample SD."),
    "stability": ("Which positions change their answer?",
                  "Inspect positions with the most move variation. The score panel uses the sample "
                  "standard deviation of non-mate evaluations. Each chart uses the highest worker count with repeat observations."),
    "stockfish": ("How do the chosen moves compare with Stockfish?",
                  "The move gap is measured in Stockfish’s own score scale for ranked moves with "
                  "numeric scores. Use top-five coverage to see how much that average represents."),
}


def describe_settings(settings):
    labels = {
        "experiment": lambda v: Path(v).name, "phase": str,
        "condition": str, "bucket_size": lambda v: f"{int(v)} entries/bucket",
        "depth": lambda v: f"depth {int(v)}", "hash_mb": lambda v: f"{int(v)} MiB",
        "cap_helpers": lambda v: "capped" if v else "uncapped",
        "rotate_root_moves": lambda v: "rotated" if v else "standard order",
        "cpu_binding": lambda v: f"requested binding: {v}",
        "applied_cpu_binding": lambda v: f"applied: {v}",
        "binary_sha256": lambda v: f"build {str(v)[:8]}",
        "threads": lambda v: f"{int(v)} workers",
    }
    return " · ".join(labels[key](value) for key, value in settings.items() if pd.notna(value))


def polish_axis(ax, title, ylabel, threads):
    ax.set_title(title, loc="left", fontweight="bold", pad=12)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("Search workers")
    ax.set_xscale("log", base=2)
    ax.set_xticks(threads, [str(int(thread)) for thread in threads])
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.grid(axis="y", color="#dae2e9", linewidth=.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, pad=7)


def line_panel(ax, metrics, show_legend=True):
    colors = ["#007e79", "#c45b32", "#494d96", "#585f65", "#916523"]
    markers = ["o", "s", "^", "D", "v"]
    for i, (label, rows, column, sd) in enumerate(metrics):
        if column not in rows:
            continue
        rows = rows.dropna(subset=[column]).sort_values("threads")
        if rows.empty:
            continue
        yerr = rows[sd].fillna(0) if sd and sd in rows and SHOW_ERROR_BARS else None
        ax.errorbar(rows["threads"], rows[column], yerr=yerr, label=label,
                    color=colors[i % len(colors)], marker=markers[i % len(markers)],
                    linewidth=1.9, markersize=5, elinewidth=.8, capsize=3)
    handles, labels = ax.get_legend_handles_labels()
    if show_legend and labels:
        ax.legend(frameon=False, fontsize=8)
    if not ax.has_data():
        ax.text(.5, .5, "No matching measurement", transform=ax.transAxes,
                ha="center", color="#697784")


def build_views(summary, positions, quality, comparisons, output_dir, tt_runs=None):
    """One selectable chart per question and set of held conditions."""
    views = {question: [] for question in QUESTIONS}
    output_dir.joinpath("charts").mkdir(exist_ok=True)

    def save(question, label, fig, note=""):
        filename = f"charts/{question}-{len(views[question]) + 1:03}.png"
        fig.savefig(output_dir / filename, dpi=FIGURE_DPI, facecolor="white")
        plt.close(fig)
        views[question].append((label, filename, note))

    def panels():
        return plt.subplots(1, 2, figsize=(10.6, 3.6), layout="constrained")

    fixed = [field for field in CONFIG if field != "threads"]
    # Include work by default when using settings from older checkouts.
    enabled = lambda key: CHART_GROUPS.get(key, True)
    for keys, rows in summary.groupby(fixed, dropna=False):
        rows = rows.sort_values("threads")
        settings = dict(zip(fixed, keys))
        label = describe_settings(settings)
        threads = rows["threads"].tolist()
        counts = ", ".join(f"{int(t)}w: {int(n)}" for t, n in zip(threads, rows["repetitions"]))
        best = rows.loc[rows["wall_mean_s"].idxmin()]
        note = (f"Fastest measured mean: {best['wall_mean_s']:.3g} s at {int(best['threads'])} workers. "
                f"Repetitions by worker count: {counts}.")
        if len(rows) > 1 and enabled("scaling"):
            fig, (wall, rate) = panels()
            polish_axis(wall, "Time per suite", "Seconds", threads)
            polish_axis(rate, "Aggregate throughput", "Million nodes / s", threads)
            line_panel(wall, [("", rows, "wall_mean_s", "wall_sd_s")])
            rates = rows.assign(million_nps=rows["nodes_per_s_mean"] / 1e6)
            line_panel(rate, [("", rates, "million_nps", None)])
            save("scaling", label, fig, note)

        if len(rows) > 1 and enabled("work"):
            fig, (main, helpers) = panels()
            polish_axis(main, "Main worker", "Million nodes", threads)
            polish_axis(helpers, "Helpers", "Million nodes", threads)
            work = rows.copy()
            for field in ("main_nodes_mean", "main_nodes_sd", "helper_nodes_mean",
                          "helper_over_depth_nodes_mean"):
                if field in work:
                    work[field] /= 1e6
            line_panel(main, [("", work, "main_nodes_mean", "main_nodes_sd")])
            line_panel(helpers, [("All helper nodes", work, "helper_nodes_mean", None),
                                       ("Beyond target depth", work, "helper_over_depth_nodes_mean", None)])
            save("work", label, fig, note)

        if "timed_repetitions" in rows and rows["timed_repetitions"].any() and enabled("phases"):
            timed = rows.loc[rows["timed_repetitions"] > 0]
            fig, (total, overhead) = panels()
            x = np.arange(len(timed))
            # Use means over the same timed rows, including when legacy runs are present.
            segments = [
                ("Helper launch", timed["launch_mean_s"], "#007e79"),
                ("Remaining time to depth", timed["depth_mean_s"] - timed["launch_mean_s"], "#b8dcd7"),
                ("Other time after depth", timed["after_depth_mean_s"] - timed["join_mean_s"], "#dce3ea"),
                ("Stop to join", timed["join_mean_s"], "#c45b32"),
            ]
            bottom = np.zeros(len(x))
            for segment, values, color in segments:
                total.bar(x, values, bottom=bottom, label=segment, color=color, width=.65)
                bottom += values.to_numpy()
            for index, elapsed in enumerate(bottom):
                total.annotate(f"{elapsed:.3g} s", (index, elapsed), xytext=(0, 4),
                               textcoords="offset points", ha="center", fontsize=8)
            total.margins(y=.19)
            total.set(title="Measured wall time", ylabel="Seconds per suite", xlabel="Search workers")
            total.set_xticks(x, timed["threads"].astype(int).astype(str))
            total.spines[["top", "right"]].set_visible(False)
            total.legend(frameon=False, fontsize=8)
            polish_axis(overhead, "Launch and shutdown share", "% of measured wall time", timed["threads"])
            line_panel(overhead, [("Launch", timed, "launch_share_mean_pct", None),
                                         ("Stop to join", timed, "join_share_mean_pct", None)])
            counts = ", ".join(f"{int(t)}w: {int(n)}" for t, n in
                               zip(timed["threads"], timed["timed_repetitions"]))
            note = f"Repetitions with phase data: {counts}."
            if "helper_stop_to_join_mean_ms" in positions:
                highest = timed.loc[timed["threads"].idxmax()]
                mask = (positions[CONFIG] == highest[CONFIG]).all(axis=1)
                tails = positions.loc[mask].nlargest(3, "helper_stop_to_join_mean_ms")
                tails = tails.loc[tails["helper_stop_to_join_mean_ms"] > 0]
                if not tails.empty:
                    tail_list = ", ".join(
                        f"#{int(row.position)} ({row.helper_stop_to_join_mean_ms:.2g} ms)"
                        for row in tails.itertuples())
                    note += (f" Longest mean shutdown at {int(highest['threads'])} workers: "
                             f"{tail_list}.")
            save("phases", label, fig, note)

        if quality is not None and len(rows) > 1 and enabled("stockfish"):
            result = quality.merge(rows[CONFIG], on=CONFIG, validate="one_to_one")
            if not result.empty:
                fig, (agreement, gap) = panels()
                polish_axis(agreement, "Reference rank of the Weiss move", "Agreement (%)", threads)
                agreement.set_ylim(0, 105)
                line_panel(agreement, [(f"Top {rank}", result, f"top{rank}_mean_pct", None)
                                               for rank in (1, 3, 5)])
                polish_axis(gap, "Stockfish best − chosen", "Stockfish cp", threads)
                line_panel(gap, [("Scored moves", result, "stockfish_move_gap_mean_cp",
                                          "stockfish_move_gap_sd_cp")])
                save("stockfish", label, fig, note)

    if enabled("across"):
        series_keys = ["experiment", "phase", "binary_sha256"]
        controls = [column for column in CONFIG if column not in (*series_keys, "threads")]
        eligible = summary.dropna(subset=["speedup_vs_1_thread"])
        for keys, rows in eligible.groupby(controls, dropna=False):
            if rows["experiment"].nunique() < 2:
                continue
            series = []
            for (experiment, phase, binary), points in rows.groupby(series_keys, dropna=False):
                if len(points) > 1:
                    label = f"{Path(experiment).name} / {phase} / {binary[:8]}"
                    series.append((label, points))
            if len({str(points["experiment"].iloc[0]) for _, points in series}) < 2:
                continue
            fig, (speedup, wall) = panels()
            fig.set_figheight(4.25)
            threads = sorted(rows["threads"].unique())
            polish_axis(speedup, "Within-batch speedup", "One worker / N workers", threads)
            polish_axis(wall, "Absolute runtime", "Seconds per suite", threads)
            for ax, column, sd in [(speedup, "speedup_vs_1_thread", None),
                                   (wall, "wall_mean_s", "wall_sd_s")]:
                line_panel(ax, [(label, points, column, sd) for label, points in series], show_legend=False)
            handles, labels = speedup.get_legend_handles_labels()
            fig.legend(handles, labels, loc="outside lower center", frameon=False,
                       fontsize=8, ncols=min(len(labels), 2))
            save("across", describe_settings(dict(zip(controls, keys))), fig,
                 "Lines use their own completed repetitions. Compare the curve shape within each batch.")

    if enabled("stability"):
        for keys, rows in positions.groupby(fixed, dropna=False):
            rows = rows.loc[rows["observations"] > 1]
            if rows.empty:
                continue
            workers = rows["threads"].max()
            rows = rows.loc[rows["threads"] == workers]
            repeated = rows.loc[(rows["observations"] > 1)
                                & (rows["modal_move_pct"] < 100)]
            if repeated.empty and rows["weiss_cp_sd"].fillna(0).gt(0).any():
                repeated = rows.loc[rows["observations"] > 1]
            if repeated.empty:
                continue
            # Show the complete drill-down table for all 50 positions via the CSV.
            worst = repeated.sort_values(["modal_move_pct", "weiss_cp_sd"],
                                         ascending=[True, False]).head(8).iloc[::-1]
            fig, (moves, scores) = panels()
            for ax in (moves, scores):
                ax.spines[["top", "right"]].set_visible(False)
                ax.set_axisbelow(True)
                ax.grid(axis="x", alpha=.2)
            ticks = [f"Position {int(pos)}" for pos in worst["position"]]
            moves.barh(ticks, worst["modal_move_pct"], color="#007e79")
            moves.set(xlabel="Most frequent move (%)", xlim=(0, 100), title="Move agreement")
            scores.barh(ticks, worst["weiss_cp_sd"], color="#c45b32")
            scores.set(xlabel="Sample standard deviation (cp)", title="Score spread")
            settings = {**dict(zip(fixed, keys)), "threads": workers}
            save("stability", describe_settings(settings), fig,
                 "Eight positions with lowest move consensus. See the position table for move counts and mate observations.")

    if not comparisons.empty and enabled("comparisons"):
        for setting, subset in comparisons.groupby("setting"):
            if setting == "threads":
                continue
            fixed = [col for col in CONFIG if col not in (setting, "threads")
                     and col in subset and subset[col].notna().any()]
            for keys, rows in subset.groupby(fixed, dropna=False):
                line_keys = ["control", "variant"]
                line_keys += [col for col in subset if col.endswith(("_variant", "_control"))
                              and col.startswith(("binary_sha256", "applied_cpu_binding"))
                              and subset[col].notna().any()]
                lines = []
                for line, points in rows.groupby(line_keys, dropna=False):
                    suffix = " / ".join(str(value)[:8] for value in line[2:] if pd.notna(value))
                    lines.append((f"{line[1]} vs {line[0]}" + (f" · {suffix}" if suffix else ""), points))
                threads = sorted(rows["threads"].unique())
                fig, (wall, work) = panels()
                polish_axis(wall, "Wall-time change", "Change vs control (%)", threads)
                polish_axis(work, "Main-worker node change", "Change vs control (%)", threads)
                for ax, metric in [(wall, "wall_ns"), (work, "main_nodes")]:
                    ax.axhline(0, color="#81909e", linewidth=.8)
                    line_panel(ax, [(name, points, f"{metric}_change_pct_mean",
                                          f"{metric}_change_pct_sd") for name, points in lines],
                               show_legend=False)
                handles, labels = wall.get_legend_handles_labels()
                fig.legend(handles, labels, loc="outside lower center", frameon=False,
                           fontsize=8, ncols=min(len(labels), 3))
                name = {"hash_mb": "TT memory", "bucket_size": "Bucket layout",
                        "cap_helpers": "Helper cap", "rotate_root_moves": "Root move order",
                        "cpu_binding": "CPU binding", "condition": "Source build",
                        "depth": "Depth"}.get(setting, setting)
                save("comparisons", f"{name} · {describe_settings(dict(zip(fixed, keys)))}", fig,
                     f"Matched repeats per point: {rows['pairs'].min()}–{rows['pairs'].max()}.")
    if enabled("tt") and tt_runs is not None and not tt_runs.empty:
        fixed = [f for f in CONFIG if f not in ("bucket_size", "threads", "binary_sha256")]
        for keys, sample in tt_runs.groupby(fixed + ["workers"], dropna=False):
            fig, axes = plt.subplots(2, 2, figsize=(10.6, 6.8), layout="constrained")
            threads = sorted(sample["threads"].unique())
            for ax, (metric, title, unit) in zip(axes.flat, [
                    ("full_miss_per_1000_probes", "Full bucket on miss", "Per 1,000 probes"),
                    ("ab_cutoff_pct", "Alpha-beta score cutoffs", "% of alpha-beta probes"),
                    ("mean_replaced_depth", "Depth of displaced entries", "Plies"),
                    ("changed_slot_pct", "Slot changed before store", "% of sampled stores")]):
                polish_axis(ax, title, unit, threads)
                lines = []
                for (width, digest), group in sample.groupby(["bucket_size", "binary_sha256"]):
                    points = group.groupby("threads")[metric].agg(["mean", "std"]).reset_index()
                    lines.append((f"{width} entries · {digest[:8]}", points, "mean", "std"))
                line_panel(ax, lines)
            label = describe_settings(dict(zip(fixed, keys))) + " · " + keys[-1]
            save("tt", label, fig, "Each point averages whole-suite rates across repetitions. "
                 "A slot change can also arise through same-worker recursion.")
    return views


def write_index(output_dir, inputs, views, tables, run_summary):
    escape = html.escape
    sections = []
    navigation = []
    nav_labels = {
        "scaling": "Scaling", "across": "Across batches", "work": "Search work", "tt": "TT diagnostics",
        "phases": "Phase timings", "comparisons": "Design changes",
        "stability": "Position stability", "stockfish": "Stockfish",
    }
    for key, (title, description) in QUESTIONS.items():
        navigation.append(f'<a href="#{key}">{escape(nav_labels[key])}</a>')
        options, figures = [], []
        for index, (label, filename, note) in enumerate(views[key]):
            options.append(f'<option value="{index}">{escape(label)}</option>')
            hidden = "" if index == 0 else " hidden"
            figures.append(
                f'<figure data-view="{index}"{hidden}><a href="{escape(filename)}">'
                f'<img src="{escape(filename)}" alt="{escape(title)} — {escape(label)}" loading="lazy"></a>'
                f'<figcaption>{escape(note)}<small>{escape(label)}</small></figcaption></figure>')
        if options:
            controls = (f'<label for="{key}-view">Comparison settings</label>'
                        f'<select id="{key}-view">{"".join(options)}</select>')
            content = controls + "".join(figures)
        elif not CHART_GROUPS.get(key, True):
            content = '<p class="empty">Turn on this section in CHART_GROUPS to generate its charts.</p>'
        else:
            message = {
                "phases": "These experiments have no phase timing records. Run the phase timing build to populate this view.",
                "comparisons": "These inputs have no matched variant and control repetitions for the configured comparisons.",
                "across": "No two batches have a matching control configuration with a one-worker measurement.",
            }.get(key, "The selected experiments have no comparable measurements for this view.")
            content = f'<p class="empty">{escape(message)}</p>'
        sections.append(f'<section id="{key}"><h2>{escape(title)}</h2>'
                        f'<p class="description">{escape(description)}</p>{content}</section>')

    source_list = "\n".join(f"<li>{escape(str(path))}</li>" for path in inputs)
    table_links = "".join(f'<li><a href="{name}.csv">{escape(label)}</a></li>'
                          for name, (_, label) in tables.items())
    repetitions = int(run_summary["repetitions"].sum())
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weiss experiment analysis</title>
<style>
:root {{color-scheme:light;--ink:#18292e;--muted:#52656c;--accent:#006c66;--line:#dbe4e6;--page:#f4f7f7}}
* {{box-sizing:border-box}} body {{margin:0;background:var(--page);color:var(--ink);font:16px/1.55 system-ui,sans-serif}}
main {{max-width:1120px;margin:auto;padding:28px clamp(16px,4vw,48px) 64px}}
header {{padding:22px 0 18px;border-bottom:2px solid var(--ink)}}
h1 {{font-size:clamp(30px,5vw,51px);line-height:1.08;letter-spacing:-.04em;margin:0 0 10px}}
h2 {{font-size:clamp(20px,3vw,26px);line-height:1.2;letter-spacing:-.02em;margin:0 0 7px}}
p {{margin:0}} header p,.description,figcaption {{color:var(--muted)}}
nav {{display:flex;flex-wrap:wrap;gap:8px;padding:20px 0 8px}}
nav a {{background:white;border:1px solid var(--line);border-radius:6px;padding:5px 10px;text-decoration:none;font-size:14px}}
a {{color:var(--accent)}} section {{background:white;padding:clamp(16px,3vw,30px);margin-top:16px;border:1px solid var(--line);border-radius:10px}}
label {{display:block;font-size:13px;font-weight:650;margin:16px 0 4px}}
select {{width:100%;padding:8px 10px;border:1px solid #aebfc2;border-radius:5px;background:white;color:var(--ink);font:inherit}}
figure {{margin:15px 0 0}} figure img {{display:block;width:100%;height:auto}}
figcaption {{font-size:14px;margin-top:3px}} figcaption small {{display:block;margin-top:4px;overflow-wrap:anywhere}} ul {{padding-left:21px;overflow-wrap:anywhere}}
.empty {{margin-top:13px;background:var(--page);padding:12px;border-radius:5px}}
details {{margin-top:15px}} summary {{font-weight:650}}
</style></head>
<body><main><header><h1>Weiss experiment analysis</h1>
<p>{repetitions:,} completed benchmark runs across {len(inputs)} batches and {len(run_summary)} configurations</p></header>
<nav aria-label="Research questions">{"".join(navigation)}<a href="#data">Data &amp; methods</a></nav>
{"".join(sections)}
<section id="data"><h2>Data &amp; methods</h2><ul>{table_links}</ul>
<details><summary>How to read these results</summary><ul>
<li>Wall time includes helper startup and shutdown. The phase view shows the subset with timing records.</li>
<li>Samples are complete benchmark runs. Error bars, when enabled, show one sample standard deviation.</li>
<li>Counts of helper nodes do not measure duplicated work. Measuring overlap, useful TT hits, or inconsistent TT writes requires engine telemetry.</li>
<li>Stockfish move gaps use saved numeric scores. Its reference analysis is finite depth. Weiss and Stockfish may use different evaluation scales.</li>
</ul></details><details><summary>Input folders</summary><ul>{source_list}</ul></details></section>
</main><script>
for (const control of document.querySelectorAll('section select')) {{
  control.addEventListener('change', () => {{
    for (const figure of control.closest('section').querySelectorAll('[data-view]')) {{
      figure.hidden = figure.dataset.view !== control.value;
    }}
  }});
}}
</script></body></html>"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main():
    args = parse_args()
    inputs = [path.expanduser() for path in args.experiments]
    inputs = [(path if path.is_absolute() else ROOT / path).resolve() for path in inputs]
    output_dir = args.output.expanduser() if args.output else inputs[0] / "analysis"
    output_dir = (output_dir if output_dir.is_absolute() else ROOT / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runs, positions = read_experiments(inputs)
    runs, positions = apply_filters(runs, positions)
    run_summary = make_run_summary(runs)

    reference, compared = None, None
    if not args.no_reference and args.reference is not None:
        reference_path = args.reference.expanduser()
        reference_path = (reference_path if reference_path.is_absolute() else ROOT / reference_path).resolve()
        if not reference_path.is_file():
            raise FileNotFoundError(f"Stockfish reference CSV not found: {reference_path}; pass --no-reference to skip")
        reference = load_reference(reference_path)
        compared = compare(positions, reference)

    position_summary = make_position_summary(positions, compared)
    stability = make_stability_summary(position_summary)
    pairs, pair_summary, position_pairs = matched_comparisons(runs, positions)
    tables = {
        "configuration_summary": (run_summary, "Configuration means, variation and phase costs"),
        "position_summary": (position_summary, "Per-position time, nodes, scores and moves"),
        "stability_summary": (stability, "Move and score stability by configuration"),
    }
    tt_runs = tt_diagnostics(positions, CONFIG + ["run", "repetition"])
    if not tt_runs.empty:
        tables["tt_by_run"] = (tt_runs, "TT counts, occupancy and rates per repetition and worker role")
        tt_positions = tt_diagnostics(positions, CONFIG + ["position"])
        tables["tt_by_position"] = (tt_positions,
                                    "TT counts and rates for each position pooled across repetitions")
        # The final histogram is shared by both roles. Give each FEN one vote
        # after pooling its repeats, even if the last block was interrupted.
        occupancy = tt_positions.loc[tt_positions.workers == "main"].groupby(CONFIG)["occupied_entries_pct"]
        capacity = occupancy.agg(positions="size", mean_pct="mean", median_pct="median",
                                 min_pct="min", max_pct="max",
                                 positions_at_least_95_pct=lambda pct: (pct >= 95).sum(),
                                 positions_below_50_pct=lambda pct: (pct < 50).sum()).reset_index()
        tables["tt_capacity"] = (capacity, "End-of-search slot occupancy across positions (equal weight per FEN)")
    if not pairs.empty:
        tables["matched_runs"] = (pairs, "Matched repetitions and their measured differences")
        tables["matched_summary"] = (pair_summary, "Matched effects by configuration")
        tables["matched_positions"] = (position_pairs, "Per-position differences for matched repetitions")
    stockfish_summary = None
    if compared is not None:
        stockfish_summary = make_stockfish_summary(compared)
        groups = CONFIG + ["run_key", "repetition"]
        tables["stockfish_observations"] = (compared, "Stockfish scores and ranks for each returned move")
        tables["stockfish_repetitions"] = (summarize(compared, groups), "Reference comparison by repetition")
        tables["stockfish_configuration_summary"] = (stockfish_summary, "Reference agreement by configuration")
        common = compared.groupby(["experiment", "position"])["move_gap_cp"].transform(
            lambda scores: scores.notna().all())
        tables["stockfish_common_positions"] = (summarize(compared.loc[common], CONFIG),
                                                "Comparison on positions scored in every run")
        missing = compared.loc[~compared["move_in_top5"]].groupby(
            CONFIG + ["position", "bestmove"]).size().rename("observations").reset_index()
        tables["stockfish_unscored_moves"] = (missing, "Returned moves outside the reference top five")

    selfplay = []
    for directory in inputs:
        status_file = directory / "selfplay-status.txt"
        summary_file = directory / "selfplay-summary.csv"
        if status_file.is_file() or summary_file.is_file():
            result = pd.read_csv(summary_file) if summary_file.is_file() else pd.DataFrame([{}])
            result["experiment"] = str(directory)
            result["status"] = status_file.read_text().strip() if status_file.is_file() else "unknown"
            selfplay.append(result)
    if selfplay:
        tables["selfplay_smoke"] = (pd.concat(selfplay, ignore_index=True),
                                    "Self-play smoke results and recorded status")

    for name, (table, _) in tables.items():
        save_csv(table, output_dir / f"{name}.csv")
    views = build_views(run_summary, position_summary, stockfish_summary, pair_summary, output_dir, tt_runs)
    write_index(output_dir, inputs, views, tables, run_summary)
    print(f"Analysed {runs['run_key'].nunique()} completed benchmark repetitions across "
          f"{len(run_summary)} configurations.")
    print(f"Saved charts, CSV summaries and dashboard: {output_dir / 'index.html'}")
    if compared is None:
        print("Stockfish comparisons were skipped.")
    else:
        print(f"Stockfish reference: {reference_path}")


if __name__ == "__main__":
    main()
