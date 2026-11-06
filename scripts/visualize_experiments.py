#!/usr/bin/env python3
"""Summarize Weiss experiment CSVs and generate a local HTML/PNG analysis suite.

Example:
    python3 scripts/visualize_experiments.py results/experiments/20261028T143204152830Z
    python3 scripts/visualize_experiments.py results/experiments/run-a results/experiments/run-b \
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
CONFIG = ["bucket_size", "depth", "threads", "hash_mb", "cap_helpers", "binary_sha256"]
NUMERIC_RUN = ["run", "repetition", "bucket_size", "depth", "threads", "hash_mb",
               "cap_helpers", "wall_ns", "cpu_ns", "main_nodes", "nodes"]
NUMERIC_POSITION = ["run", "repetition", "bucket_size", "depth", "threads", "hash_mb",
                    "cap_helpers", "position", "wall_ns", "cpu_ns", "main_nodes", "nodes", "score"]

# ============================== EDIT SETTINGS HERE =============================
# Put experiment folders here to run the script without command-line arguments.
# Example: [ROOT / "results/experiments/20261028T143204152830Z"]
EXPERIMENT_DIRS = []
REFERENCE_CSV = DEFAULT_REFERENCE  # Set to None to skip Stockfish comparisons.
OUTPUT_DIR = None                 # None writes to analysis/ inside the first experiment.

# Keep all values (None means include every available value).
FILTERS = {
    "bucket_size": None,          # Example: [2]
    "depth": None,                # Example: [15, 20, 23]
    "threads": None,              # Example: [1, 4, 8, 32, 128, 192]
    "hash_mb": None,              # Example: [2, 64, 512]
    "cap_helpers": None,          # Example: [0] for uncapped, [1] for capped
}

CHART_GROUPS = {"scaling": True, "stockfish": True, "stability": True}
SHOW_ERROR_BARS = True            # ±1 sample standard deviation
PLOT_COLOR_MAP = "turbo"
FIGURE_DPI = 150

# Each point is still a configuration mean. Remove a tuple here to hide that panel,
# or edit its column/title/axis labels to customize a chart.
PERFORMANCE_METRICS = [
    ("wall_mean_s", "wall_sd_s", "Mean time to requested depth", "Seconds for the 50-position suite"),
    ("nodes_mean", "nodes_sd", "Mean total nodes", "Nodes summed over all workers"),
    ("main_nodes_mean", "main_nodes_sd", "Mean main-worker nodes", "Nodes searched by main worker"),
    ("cpu_mean_s", "cpu_sd_s", "Mean process CPU time", "CPU seconds summed over workers"),
    ("speedup_vs_1_thread", None, "Speedup versus one worker", "One-worker time / configuration time"),
    ("scaling_efficiency_pct", None, "Parallel efficiency", "Speedup / workers × 100 (%)"),
]
STOCKFISH_METRICS = [
    ("absolute_score_gap_mean_cp", "absolute_score_gap_sd_cp", "Score gap from Stockfish best", "Mean |Weiss CP − SF19 best CP| (cp-only)"),
    ("signed_score_gap_mean_cp", "signed_score_gap_sd_cp", "Signed score gap", "Mean Weiss CP − SF19 best CP (cp-only)"),
    ("top1_mean_pct", "top1_sd_pct", "Exact best-move agreement", "Chosen move is Stockfish rank 1 (%)"),
    ("top3_mean_pct", "top3_sd_pct", "Move in Stockfish top 3", "Chosen move is Stockfish rank 1–3 (%)"),
    ("top5_mean_pct", "top5_sd_pct", "Move in Stockfish top 5", "Chosen move is Stockfish rank 1–5 (%)"),
    ("stockfish_move_gap_mean_cp", "stockfish_move_gap_sd_cp", "Stockfish score gap for chosen move", "SF best CP − chosen-move CP; top-five scored moves"),
]
STABILITY_METRICS = [
    ("score_sd_mean_cp", "score_sd_between_positions_cp", "Within-position score variation", "Mean CP sample SD across repeated searches"),
    ("distinct_moves_mean", "distinct_moves_sd", "Move diversity", "Mean distinct moves per position across repetitions"),
    ("modal_move_mean_pct", "modal_move_sd_pct", "Move consensus", "Mean modal-move frequency per position (%)"),
]
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
            if "binary_sha256" not in frame:
                frame["binary_sha256"] = "unknown"
            frame["binary_sha256"] = frame["binary_sha256"].fillna("unknown").astype(str)
            numeric_columns(frame, NUMERIC_RUN if label == "runs.csv" else NUMERIC_POSITION)
            if frame[CONFIG + ["run", "repetition"]].isna().any().any():
                raise ValueError(f"Missing configuration or run identity in {source / label}")
            frame["experiment"] = source.name
            frame["run_key"] = source.as_posix() + "::" + frame["run"].astype(int).astype(str)
        run_frames.append(runs)
        position_frames.append(positions)

    runs = pd.concat(run_frames, ignore_index=True)
    positions = pd.concat(position_frames, ignore_index=True)
    if runs.empty or positions.empty:
        raise ValueError("Experiment CSVs must contain at least one complete run")
    if runs["run_key"].duplicated().any():
        raise ValueError("A run ID occurs more than once within an experiment folder")
    if positions.duplicated(["run_key", "position"]).any():
        raise ValueError("A run contains duplicate position rows")
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
    if reference.duplicated(["position", "move"]).any():
        raise ValueError("Reference CSV contains duplicate position/move rows")
    for position, group in reference.groupby("position"):
        if sorted(group["rank"].astype(int).tolist()) != [1, 2, 3, 4, 5]:
            raise ValueError(f"Reference position {position} does not contain ranks 1..5")

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


def compare_stockfish(positions, reference):
    best = reference.loc[reference["rank"] == 1,
                         ["position", "move", "score_cp", "score_type"]].rename(
        columns={"move": "sf_bestmove", "score_cp": "sf_best_cp", "score_type": "sf_best_score_type"})
    candidates = reference[["position", "move", "rank", "score_cp", "score_type"]].rename(
        columns={"move": "sf_candidate_move", "rank": "sf_move_rank",
                 "score_cp": "sf_chosen_move_cp", "score_type": "sf_chosen_move_score_type"})
    rows = positions.merge(best, on="position", how="left", validate="many_to_one")
    rows = rows.merge(candidates, left_on=["position", "bestmove"],
                      right_on=["position", "sf_candidate_move"], how="left", validate="many_to_one")
    weiss_cp = rows["score"].where(rows["score_type"] == "cp")
    rows["score_gap_cp"] = weiss_cp - rows["sf_best_cp"]
    rows["absolute_score_gap_cp"] = rows["score_gap_cp"].abs()
    rows["stockfish_move_gap_cp"] = rows["sf_best_cp"] - rows["sf_chosen_move_cp"]
    rows["top1_agrees"] = rows["bestmove"].eq(rows["sf_bestmove"])
    rows["top3_coverage"] = rows["sf_move_rank"].le(3).fillna(False)
    rows["top5_coverage"] = rows["sf_move_rank"].le(5).fillna(False)
    return rows


def make_run_summary(runs):
    data = runs.copy()
    data["wall_s"] = data["wall_ns"] / 1e9
    data["cpu_s"] = data["cpu_ns"] / 1e9
    data["nodes_per_s"] = data["nodes"] / data["wall_s"]
    aggregations = dict(
        repetitions=("run_key", "nunique"),
        wall_mean_s=("wall_s", "mean"), wall_sd_s=("wall_s", "std"),
        cpu_mean_s=("cpu_s", "mean"), cpu_sd_s=("cpu_s", "std"),
        nodes_mean=("nodes", "mean"), nodes_sd=("nodes", "std"),
        main_nodes_mean=("main_nodes", "mean"), main_nodes_sd=("main_nodes", "std"),
        nodes_per_s_mean=("nodes_per_s", "mean"),
    )
    if "process_wall_ns" in data:
        data["process_wall_s"] = data["process_wall_ns"] / 1e9
        aggregations["process_wall_mean_s"] = ("process_wall_s", "mean")
    summary = data.groupby(CONFIG, dropna=False).agg(**aggregations).reset_index()
    paired = summary.loc[summary["threads"] == 1,
                         ["bucket_size", "depth", "hash_mb", "cap_helpers", "binary_sha256", "wall_mean_s"]]
    paired = paired.rename(columns={"wall_mean_s": "one_thread_wall_mean_s"})
    summary = summary.merge(paired, on=["bucket_size", "depth", "hash_mb", "cap_helpers", "binary_sha256"],
                            how="left", validate="many_to_one")
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
            weiss_cp_mean=cp.mean(), weiss_cp_sd=cp.std(),
            mate_observations=int((group["score_type"] == "mate").sum()),
            distinct_moves=len(moves), modal_move_pct=100 * moves.iloc[0] / len(group),
            moves_returned="; ".join(f"{move} ({count})" for move, count in moves.items()),
        )
        rows.append(row)
    result = pd.DataFrame(rows)
    if compared is not None:
        quality = []
        for keys, group in compared.groupby(CONFIG + ["position"], dropna=False, sort=False):
            row = dict(zip(CONFIG + ["position"], keys))
            row.update(
                top1_agreement_pct=100 * group["top1_agrees"].mean(),
                top3_coverage_pct=100 * group["top3_coverage"].mean(),
                top5_coverage_pct=100 * group["top5_coverage"].mean(),
                stockfish_move_gap_cp_mean=group["stockfish_move_gap_cp"].mean(),
                absolute_score_gap_cp_mean=group["absolute_score_gap_cp"].mean(),
                score_gap_cp_count=int(group["score_gap_cp"].notna().sum()),
                stockfish_ranked_move_count=int(group["sf_move_rank"].notna().sum()),
            )
            quality.append(row)
        result = result.merge(pd.DataFrame(quality), on=CONFIG + ["position"], how="left",
                              validate="one_to_one")
    return result


def make_stockfish_summary(compared):
    # First average the 50 positions within each independent benchmark repetition.
    per_run = compared.groupby(["run_key"] + CONFIG, dropna=False).agg(
        top1_agreement_pct=("top1_agrees", lambda values: 100 * values.mean()),
        top3_coverage_pct=("top3_coverage", lambda values: 100 * values.mean()),
        top5_coverage_pct=("top5_coverage", lambda values: 100 * values.mean()),
        mean_absolute_score_gap_cp=("absolute_score_gap_cp", "mean"),
        mean_signed_score_gap_cp=("score_gap_cp", "mean"),
        mean_stockfish_move_gap_cp=("stockfish_move_gap_cp", "mean"),
        cp_comparisons=("score_gap_cp", "count"),
        stockfish_scored_chosen_moves=("stockfish_move_gap_cp", "count"),
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


def safe_token(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-") or "unknown"


def plot_bundle(group, config_label, filename, metrics, output_dir, header, notes=()):
    fig, axes = plt.subplots(2, 3, figsize=(16, 9), constrained_layout=True)
    axes = axes.flatten()
    depths = sorted(group["depth"].dropna().unique())
    cmap = plt.get_cmap(PLOT_COLOR_MAP, max(1, len(depths)))
    colors = {depth: cmap(i) for i, depth in enumerate(depths)}

    for axis, (column, sd_column, title, ylabel) in zip(axes, metrics):
        if column not in group:
            axis.set_visible(False)
            continue
        for depth in depths:
            series = group.loc[group["depth"] == depth].sort_values("threads")
            if series.empty:
                continue
            y = pd.to_numeric(series[column], errors="coerce").to_numpy(dtype=float)
            x = series["threads"].to_numpy(dtype=float)
            yerr = (pd.to_numeric(series[sd_column], errors="coerce").fillna(0).to_numpy(dtype=float)
                    if SHOW_ERROR_BARS and sd_column and sd_column in series
                    else np.zeros(len(series)))
            valid = np.isfinite(x) & np.isfinite(y)
            if not valid.any():
                continue
            if "repetitions" in series:
                n = int(series.loc[valid, "repetitions"].min())
                label = f"depth {int(depth)} (n={n} runs)"
            elif "positions" in series:
                n = int(series.loc[valid, "positions"].min())
                label = f"depth {int(depth)} (n={n} positions)"
            else:
                label = f"depth {int(depth)}"
            axis.errorbar(x[valid], y[valid], yerr=yerr[valid], marker="o", linewidth=1.7,
                          markersize=4, capsize=3, color=colors[depth], label=label)
        axis.set_title(title)
        axis.set_xlabel("Search workers (main worker included)")
        axis.set_ylabel(ylabel)
        axis.grid(True, alpha=0.25)
        axis.set_xscale("log", base=2)
        ticks = sorted(group["threads"].dropna().unique())
        axis.set_xticks(ticks, [str(int(t)) for t in ticks])
        axis.legend(fontsize=8, frameon=False)
    for axis in axes[len(metrics):]:
        axis.set_visible(False)
    subtitle = "\n".join(notes)
    fig.suptitle(f"{header}\n{config_label}" + (f"\n{subtitle}" if subtitle else ""), fontsize=13)
    fig.savefig(output_dir / filename, dpi=FIGURE_DPI, bbox_inches="tight")
    plt.close(fig)


def make_figures(run_summary, stockfish_summary, stability, output_dir):
    figure_names = []
    for keys, subset in run_summary.groupby(["bucket_size", "cap_helpers", "hash_mb", "binary_sha256"],
                                             dropna=False, sort=True):
        bucket, cap, memory, binary = keys
        label = f"bucket size {int(bucket)} | helpers capped: {bool(cap)} | TT {int(memory)} MiB | build {str(binary)[:10]}"
        token = f"b{int(bucket)}_cap{int(cap)}_h{int(memory)}_{safe_token(str(binary)[:10])}"
        if CHART_GROUPS.get("scaling", True):
            perf_metrics = PERFORMANCE_METRICS
            plot_bundle(subset, label, f"scaling_{token}.png", perf_metrics, output_dir,
                        "Performance and work volume (points are configuration means; bars are ±1 SD)")
            figure_names.append(f"scaling_{token}.png")

        if stockfish_summary is not None and CHART_GROUPS.get("stockfish", True):
            quality = stockfish_summary.loc[
                (stockfish_summary["bucket_size"] == bucket)
                & (stockfish_summary["cap_helpers"] == cap)
                & (stockfish_summary["hash_mb"] == memory)
                & (stockfish_summary["binary_sha256"] == binary)]
            if not quality.empty:
                quality = quality.merge(subset[CONFIG].drop_duplicates(),
                                        on=CONFIG, how="left", validate="one_to_one")
                quality_metrics = STOCKFISH_METRICS
                plot_bundle(quality, label, f"stockfish_{token}.png", quality_metrics, output_dir,
                            "Move agreement and reference-score comparisons (means ±1 SD across repetitions)",
                            notes=("Stockfish is a finite-depth reference, not mathematical ground truth.",
                                   "Weiss and Stockfish CP scales differ; score gaps are descriptive."))
                figure_names.append(f"stockfish_{token}.png")

        stable = stability.loc[
            (stability["bucket_size"] == bucket)
            & (stability["cap_helpers"] == cap)
            & (stability["hash_mb"] == memory)
            & (stability["binary_sha256"] == binary)]
        if not stable.empty and CHART_GROUPS.get("stability", True):
            stable = stable.merge(subset[CONFIG].drop_duplicates(),
                                  on=CONFIG, how="left", validate="one_to_one")
            stable_metrics = STABILITY_METRICS
            plot_bundle(stable, label, f"stability_{token}.png", stable_metrics, output_dir,
                        "Repeatability across runs (mean ±1 SD across positions)",
                        notes=("Mate scores are excluded from CP score variation.",))
            figure_names.append(f"stability_{token}.png")
    return figure_names


def write_index(output_dir, inputs, figure_names, has_reference):
    definitions = [
        "Each plotted point is the mean for one parameter configuration across completed repetitions; error bars show sample standard deviation.",
        "Runtime is wall-clock time for the 50-position suite, which is the measure of time to reach the requested depth. CPU time is summed over workers.",
        "Total nodes sum all workers; main-worker nodes show the root worker separately. Rising total nodes with falling or flat main-worker nodes means the helpers are adding work; it does not by itself mean faster progress.",
        "Speedup compares the mean wall time with the matching one-worker configuration (same depth, TT, bucket, helper cap and build). Efficiency is speedup divided by worker count.",
        "Move stability is calculated per position across repetitions, then averaged over positions. CP standard deviation excludes mate scores.",
    ]
    if has_reference:
        definitions += [
            "Top-3/top-5 coverage means Weiss's selected move appears in Stockfish 19's depth-28 MultiPV list at rank 1–3/rank 1–5. A missing move is outside the saved list, not proof it is bad.",
            "Stockfish move gap compares the Stockfish best score with the Stockfish score for the selected Weiss move; it is only available when that move appears in the saved top five.",
            "Score gap compares Weiss's root CP score with Stockfish's rank-1 CP score. Different engine evaluation scales and finite search depths make this descriptive, not an objective error measurement.",
        ]
    cards = "\n".join(
        f'<figure><a href="{html.escape(name)}"><img src="{html.escape(name)}" alt="{html.escape(name)}"></a>'
        f'<figcaption>{html.escape(name)}</figcaption></figure>' for name in figure_names)
    source_list = "\n".join(f"<li>{html.escape(str(path))}</li>" for path in inputs)
    definition_list = "\n".join(f"<li>{html.escape(text)}</li>" for text in definitions)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weiss experiment analysis</title><style>
body{{font:16px/1.5 system-ui,-apple-system,sans-serif;max-width:1500px;margin:32px auto;padding:0 24px;color:#172033;background:#f5f7fb}}
h1,h2{{line-height:1.2}}section{{background:white;padding:18px 24px;margin:18px 0;border-radius:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:18px}}figure{{margin:0;background:white;padding:10px;border:1px solid #d8deea;border-radius:8px}}
img{{display:block;width:100%;height:auto}}figcaption{{font:14px ui-monospace,monospace;overflow-wrap:anywhere;margin:6px}}
code{{background:#eef1f7;padding:2px 5px;border-radius:4px}}
</style></head><body><h1>Weiss experiment analysis</h1>
<section><p>Generated from completed experiment CSVs. This script only analyses saved data; it does not run Weiss or Stockfish.</p>
<h2>Input experiment folders</h2><ul>{source_list}</ul>
<h2>Metric definitions and interpretation</h2><ul>{definition_list}</ul></section>
<section><h2>Charts</h2><div class="grid">{cards}</div></section>
<section><h2>Tables</h2><p><code>configuration_summary.csv</code> has run-level means and standard deviations; <code>position_summary.csv</code> exposes per-position runtime, nodes, score spread and move stability.</p>
{('<p><code>stockfish_observations.csv</code> contains the row-level matched reference results; <code>stockfish_configuration_summary.csv</code> aggregates agreement and score comparisons by configuration.</p>' if has_reference else '')}</section>
</body></html>"""
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
        compared = compare_stockfish(positions, reference)

    position_summary = make_position_summary(positions, compared)
    stability = make_stability_summary(position_summary)
    save_csv(run_summary, output_dir / "configuration_summary.csv")
    save_csv(position_summary, output_dir / "position_summary.csv")
    save_csv(stability, output_dir / "stability_summary.csv")

    stockfish_summary = None
    if compared is not None:
        stockfish_summary = make_stockfish_summary(compared)
        save_csv(compared, output_dir / "stockfish_observations.csv")
        save_csv(stockfish_summary, output_dir / "stockfish_configuration_summary.csv")

    figure_names = make_figures(run_summary, stockfish_summary, stability, output_dir)
    write_index(output_dir, inputs, figure_names, compared is not None)
    print(f"Analysed {runs['run_key'].nunique()} completed benchmark repetitions across "
          f"{len(run_summary)} configurations.")
    print(f"Saved charts, CSV summaries and dashboard: {output_dir / 'index.html'}")
    if compared is None:
        print("Stockfish comparisons were skipped.")
    else:
        print(f"Stockfish reference: {reference_path}")


if __name__ == "__main__":
    main()
