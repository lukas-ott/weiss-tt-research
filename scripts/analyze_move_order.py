#!/usr/bin/env python3
"""Create a paired HTML analysis of normal vs helper root-move rotation.

The runner records both modes in the same experiment folder. Runs are paired by
phase, repetition, and configuration; per-position results are paired by those
same fields plus position. No engine searches are started.

Usage:
    python3 scripts/analyze_move_order.py
    python3 scripts/analyze_move_order.py results/experiments/move-order-20261106T000000Z
    python3 scripts/analyze_move_order.py EXPERIMENT_DIR --output analysis-folder
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

# ========================== EDIT ANALYSIS SETTINGS HERE =========================
# Leave as None to auto-select the newest experiment containing both rotation modes.
# Or set an exact results folder in this editable setting.
EXPERIMENT_DIR = None
OUTPUT_DIR = None  # None writes to <experiment>/analysis-move-order.
FIGURE_DPI = 160
SHOW_ERROR_BARS = True  # ±1 sample SD across paired repetitions.
# ================================================================================

CONFIG = [
    "condition", "bucket_size", "phase", "depth", "threads", "hash_mb",
    "cap_helpers", "cpu_binding", "binary_sha256",
]
PAIR_KEYS = CONFIG + ["repetition"]
RUN_METRICS = ["wall_ns", "cpu_ns", "nodes", "main_nodes"]
POSITION_KEYS = PAIR_KEYS + ["position"]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "experiment", nargs="?", type=Path,
        help="Move-order experiment folder; overrides EXPERIMENT_DIR above",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Output folder; overrides OUTPUT_DIR above",
    )
    args = parser.parse_args()
    args.experiment = args.experiment or EXPERIMENT_DIR
    args.output = args.output or OUTPUT_DIR
    if args.experiment is None:
        candidates = sorted(
            (ROOT / "results" / "experiments").glob("*"),
            key=lambda path: path.stat().st_mtime if path.is_dir() else 0,
            reverse=True,
        )
        args.experiment = next(
            (path for path in candidates if (path / "runs.csv").is_file()
             and (path / "positions.csv").is_file() and has_both_rotation_modes(path)),
            None,
        )
    if args.experiment is None:
        parser.error(
            "No experiment with both root-move rotation values was found. Set ROOT_MOVE_ROTATIONS = [0, 1] "
            "in scripts/run_experiments.py, run it, "
            "or set EXPERIMENT_DIR / pass its results folder."
        )
    return args


def has_both_rotation_modes(path):
    try:
        values = pd.read_csv(path / "runs.csv", usecols=["rotate_root_moves"])
    except (OSError, ValueError, pd.errors.ParserError):
        return False
    return set(values["rotate_root_moves"].dropna().unique()) == {0, 1}


def resolve_path(path):
    path = Path(path).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def read_data(experiment_dir):
    runs_path = experiment_dir / "runs.csv"
    positions_path = experiment_dir / "positions.csv"
    if not runs_path.is_file() or not positions_path.is_file():
        raise FileNotFoundError(
            f"Expected runs.csv and positions.csv in {experiment_dir}"
        )

    runs = pd.read_csv(runs_path)
    positions = pd.read_csv(positions_path)
    for frame in (runs, positions):
        if "condition" not in frame:
            frame["condition"] = "candidate"
        if "cpu_binding" not in frame:
            frame["cpu_binding"] = "none"
        frame["cpu_binding"] = frame["cpu_binding"].fillna("none")
    required_run = set(CONFIG + ["repetition", "run", "rotate_root_moves"] + RUN_METRICS)
    required_position = set(
        POSITION_KEYS + ["rotate_root_moves", "bestmove", "score_type", "score"]
    )
    for label, frame, required in (
        ("runs.csv", runs, required_run),
        ("positions.csv", positions, required_position),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{label} is missing columns: {', '.join(sorted(missing))}")

    numeric = [
        "bucket_size", "depth", "threads", "hash_mb", "cap_helpers",
        "repetition", "run", "rotate_root_moves",
    ] + RUN_METRICS
    for column in numeric:
        if column in runs:
            runs[column] = pd.to_numeric(runs[column], errors="coerce")
    for column in numeric + ["position", "score"]:
        if column in positions:
            positions[column] = pd.to_numeric(positions[column], errors="coerce")

    for frame, label in ((runs, "runs.csv"), (positions, "positions.csv")):
        frame["binary_sha256"] = frame["binary_sha256"].fillna("unknown").astype(str)
        if frame[CONFIG + ["repetition", "rotate_root_moves"]].isna().any().any():
            raise ValueError(f"{label} has missing configuration or pairing values")
        if not set(frame["rotate_root_moves"].unique()).issubset({0, 1}):
            raise ValueError(f"{label} contains a rotation value other than 0 or 1")

    if runs["run"].duplicated().any():
        raise ValueError("runs.csv contains duplicate run IDs")
    if positions.duplicated(["run", "position"]).any():
        raise ValueError("positions.csv contains duplicate run/position rows")

    run_ids = set(runs["run"].astype(int))
    if not set(positions["run"].astype(int)).issubset(run_ids):
        raise ValueError("positions.csv contains rows without a corresponding run")

    return runs, positions


def make_paired_runs(runs):
    normal = runs.loc[runs["rotate_root_moves"] == 0].copy()
    rotated = runs.loc[runs["rotate_root_moves"] == 1].copy()

    keep = PAIR_KEYS + ["run"] + RUN_METRICS
    normal = normal[keep].rename(
        columns={"run": "run_normal"}
        | {metric: f"{metric}_normal" for metric in RUN_METRICS}
    )
    rotated = rotated[keep].rename(
        columns={"run": "run_rotated"}
        | {metric: f"{metric}_rotated" for metric in RUN_METRICS}
    )
    paired = normal.merge(
        rotated, on=PAIR_KEYS, how="inner", validate="one_to_one"
    )
    if paired.empty:
        raise ValueError(
            "No matched normal/rotated runs. The experiment needs both modes "
            "for the same configuration and repetition."
        )

    # Unmatched runs can occur if a time budget stops partway through a block.
    normal_keys = normal[PAIR_KEYS].drop_duplicates()
    rotated_keys = rotated[PAIR_KEYS].drop_duplicates()
    matched_keys = paired[PAIR_KEYS]
    unmatched_normal = len(normal_keys.merge(
        matched_keys.drop_duplicates(), on=PAIR_KEYS, how="left", indicator=True
    ).query("_merge == 'left_only'"))
    unmatched_rotated = len(rotated_keys.merge(
        matched_keys.drop_duplicates(), on=PAIR_KEYS, how="left", indicator=True
    ).query("_merge == 'left_only'"))

    paired["wall_s_normal"] = paired["wall_ns_normal"] / 1e9
    paired["wall_s_rotated"] = paired["wall_ns_rotated"] / 1e9
    paired["cpu_s_normal"] = paired["cpu_ns_normal"] / 1e9
    paired["cpu_s_rotated"] = paired["cpu_ns_rotated"] / 1e9
    paired["wall_improvement_pct"] = (
        (paired["wall_ns_normal"] - paired["wall_ns_rotated"])
        / paired["wall_ns_normal"] * 100
    )
    paired["nodes_change_pct"] = (
        (paired["nodes_rotated"] - paired["nodes_normal"])
        / paired["nodes_normal"].replace(0, np.nan) * 100
    )
    paired["main_nodes_change_pct"] = (
        (paired["main_nodes_rotated"] - paired["main_nodes_normal"])
        / paired["main_nodes_normal"].replace(0, np.nan) * 100
    )
    return paired, unmatched_normal, unmatched_rotated


def make_paired_positions(positions):
    columns = POSITION_KEYS + [
        "bestmove", "score_type", "score", "wall_ns", "nodes", "main_nodes",
    ]
    normal = positions.loc[positions["rotate_root_moves"] == 0, columns].copy()
    rotated = positions.loc[positions["rotate_root_moves"] == 1, columns].copy()

    rename_normal = {
        "bestmove": "bestmove_normal", "score_type": "score_type_normal",
        "score": "score_normal", "wall_ns": "wall_ns_normal",
        "nodes": "nodes_normal", "main_nodes": "main_nodes_normal",
    }
    rename_rotated = {
        "bestmove": "bestmove_rotated", "score_type": "score_type_rotated",
        "score": "score_rotated", "wall_ns": "wall_ns_rotated",
        "nodes": "nodes_rotated", "main_nodes": "main_nodes_rotated",
    }
    normal = normal.rename(columns=rename_normal)
    rotated = rotated.rename(columns=rename_rotated)
    paired = normal.merge(
        rotated, on=POSITION_KEYS, how="inner", validate="one_to_one"
    )
    if paired.empty:
        raise ValueError("No matched per-position results were found.")

    paired["same_move"] = paired["bestmove_normal"].eq(paired["bestmove_rotated"])
    cp_pair = (
        paired["score_type_normal"].eq("cp")
        & paired["score_type_rotated"].eq("cp")
    )
    paired["score_delta_cp"] = (
        paired["score_rotated"] - paired["score_normal"]
    ).where(cp_pair)
    paired["absolute_score_delta_cp"] = paired["score_delta_cp"].abs()
    paired["position_wall_improvement_pct"] = (
        (paired["wall_ns_normal"] - paired["wall_ns_rotated"])
        / paired["wall_ns_normal"].replace(0, np.nan) * 100
    )
    return paired


def summarize_quality(paired_positions):
    def mean_percent_same(values):
        return values.mean() * 100

    per_repetition = paired_positions.groupby(
        PAIR_KEYS, dropna=False, sort=False
    ).agg(
        positions_compared=("position", "nunique"),
        move_agreement_pct=("same_move", mean_percent_same),
        positions_with_cp_pair=("score_delta_cp", "count"),
        mean_signed_score_delta_cp=("score_delta_cp", "mean"),
        mean_absolute_score_delta_cp=("absolute_score_delta_cp", "mean"),
        mean_position_wall_improvement_pct=("position_wall_improvement_pct", "mean"),
    ).reset_index()
    return per_repetition


def summarize_configurations(paired_runs, repetition_quality):
    grouped = paired_runs.groupby(CONFIG, dropna=False, sort=True)
    summary = grouped.agg(
        paired_repetitions=("repetition", "nunique"),
        normal_wall_mean_s=("wall_s_normal", "mean"),
        normal_wall_sd_s=("wall_s_normal", "std"),
        rotated_wall_mean_s=("wall_s_rotated", "mean"),
        rotated_wall_sd_s=("wall_s_rotated", "std"),
        wall_improvement_pct_mean=("wall_improvement_pct", "mean"),
        wall_improvement_pct_sd=("wall_improvement_pct", "std"),
        normal_cpu_mean_s=("cpu_s_normal", "mean"),
        rotated_cpu_mean_s=("cpu_s_rotated", "mean"),
        normal_nodes_mean=("nodes_normal", "mean"),
        rotated_nodes_mean=("nodes_rotated", "mean"),
        nodes_change_pct_mean=("nodes_change_pct", "mean"),
        nodes_change_pct_sd=("nodes_change_pct", "std"),
        normal_main_nodes_mean=("main_nodes_normal", "mean"),
        rotated_main_nodes_mean=("main_nodes_rotated", "mean"),
        main_nodes_change_pct_mean=("main_nodes_change_pct", "mean"),
        main_nodes_change_pct_sd=("main_nodes_change_pct", "std"),
    ).reset_index()

    quality = repetition_quality.groupby(
        CONFIG, dropna=False, sort=True
    ).agg(
        move_agreement_pct_mean=("move_agreement_pct", "mean"),
        move_agreement_pct_sd=("move_agreement_pct", "std"),
        mean_signed_score_delta_cp=("mean_signed_score_delta_cp", "mean"),
        signed_score_delta_cp_sd=("mean_signed_score_delta_cp", "std"),
        mean_absolute_score_delta_cp=("mean_absolute_score_delta_cp", "mean"),
        absolute_score_delta_cp_sd=("mean_absolute_score_delta_cp", "std"),
        cp_position_pairs=("positions_with_cp_pair", "sum"),
    ).reset_index()
    return summary.merge(quality, on=CONFIG, how="left", validate="one_to_one")


def save_csv(frame, path):
    frame.to_csv(path, index=False, float_format="%.4f")


def safe_token(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-") or "unknown"


def add_errorbar(axis, rows, y, sd, label, color, linestyle="-", marker="o"):
    rows = rows.sort_values("threads")
    values = pd.to_numeric(rows[y], errors="coerce").to_numpy(dtype=float)
    x_values = rows["threads"].to_numpy(dtype=float)
    if SHOW_ERROR_BARS and sd in rows:
        errors = pd.to_numeric(rows[sd], errors="coerce").fillna(0).to_numpy(dtype=float)
    else:
        errors = np.zeros(len(rows))
    valid = np.isfinite(values) & np.isfinite(x_values)
    if valid.any():
        axis.errorbar(
            x_values[valid], values[valid], yerr=errors[valid],
            label=label, color=color, linestyle=linestyle, marker=marker,
            linewidth=1.6, markersize=4, capsize=3,
        )


def set_thread_axis(axis, rows):
    ticks = sorted(rows["threads"].dropna().unique())
    axis.set_xscale("log", base=2)
    axis.set_xticks(ticks, [str(int(value)) for value in ticks])
    axis.set_xlabel("Search workers (main worker included)")
    axis.grid(True, alpha=0.25)


def make_figures(summary, output_dir):
    performance_figures = []
    quality_figures = []
    group_columns = [
        "condition", "bucket_size", "phase", "hash_mb", "cap_helpers", "cpu_binding", "binary_sha256",
    ]
    for keys, subset in summary.groupby(group_columns, dropna=False, sort=True):
        condition, bucket, phase, memory, cap, cpu_binding, binary = keys
        depths = sorted(subset["depth"].unique())
        cmap = plt.get_cmap("turbo", max(1, len(depths)))
        colors = {depth: cmap(i) for i, depth in enumerate(depths)}

        fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
        wall_ax, improvement_ax, nodes_ax, main_nodes_ax = axes.flatten()
        for depth in depths:
            rows = subset.loc[subset["depth"] == depth]
            color = colors[depth]
            add_errorbar(
                wall_ax, rows, "normal_wall_mean_s", "normal_wall_sd_s",
                f"d{int(depth)} normal", color, "-", "o",
            )
            add_errorbar(
                wall_ax, rows, "rotated_wall_mean_s", "rotated_wall_sd_s",
                f"d{int(depth)} rotated", color, "--", "s",
            )
            add_errorbar(
                improvement_ax, rows, "wall_improvement_pct_mean",
                "wall_improvement_pct_sd", f"d{int(depth)}", color,
            )
            add_errorbar(
                nodes_ax, rows, "normal_nodes_mean", None,
                f"d{int(depth)} normal", color, "-", "o",
            )
            add_errorbar(
                nodes_ax, rows, "rotated_nodes_mean", None,
                f"d{int(depth)} rotated", color, "--", "s",
            )
            add_errorbar(
                main_nodes_ax, rows, "normal_main_nodes_mean", None,
                f"d{int(depth)} normal", color, "-", "o",
            )
            add_errorbar(
                main_nodes_ax, rows, "rotated_main_nodes_mean", None,
                f"d{int(depth)} rotated", color, "--", "s",
            )

        wall_ax.set_title("Time to requested depth (mean ± SD)")
        wall_ax.set_ylabel("Wall-clock seconds for 50 positions")
        improvement_ax.axhline(0, color="#555", linewidth=1)
        improvement_ax.set_title("Paired wall-time improvement (mean ± SD)")
        improvement_ax.set_ylabel("Positive means rotation is faster (%)")
        nodes_ax.set_title("Total nodes (mean)")
        nodes_ax.set_ylabel("Nodes from all workers")
        main_nodes_ax.set_title("Main-worker nodes (mean)")
        main_nodes_ax.set_ylabel("Nodes searched by main worker")
        for axis in axes.flatten():
            set_thread_axis(axis, subset)
            axis.legend(fontsize=8, frameon=False)

        label = (
            f"{condition} build | bucket {int(bucket)} | phase {phase} | TT {int(memory)} MiB | "
            f"helpers capped {int(cap)} | build {str(binary)[:10]}"
        )
        fig.suptitle(label, fontsize=12)
        token = safe_token(
            f"performance_{condition}_b{bucket}_{phase}_h{memory}_cap{cap}_{str(binary)[:10]}"
        )
        filename = f"{token}.png"
        fig.savefig(output_dir / filename, dpi=FIGURE_DPI, bbox_inches="tight")
        plt.close(fig)
        performance_figures.append((filename, label))

        quality_fig, quality_axes = plt.subplots(
            1, 2, figsize=(13, 5), constrained_layout=True
        )
        move_ax, score_ax = quality_axes
        for depth in depths:
            rows = subset.loc[subset["depth"] == depth]
            color = colors[depth]
            add_errorbar(
                move_ax, rows, "move_agreement_pct_mean",
                "move_agreement_pct_sd", f"d{int(depth)}", color,
            )
            add_errorbar(
                score_ax, rows, "mean_absolute_score_delta_cp",
                "absolute_score_delta_cp_sd", f"d{int(depth)}", color,
            )
        move_ax.set_ylim(0, 100)
        move_ax.set_title("Same best move in both modes (mean ± SD)")
        move_ax.set_ylabel("Paired positions with same move (%)")
        score_ax.set_title("Absolute CP-score change (mean ± SD)")
        score_ax.set_ylabel("|rotated score − normal score| (centipawns)")
        for axis in quality_axes:
            set_thread_axis(axis, subset)
            axis.legend(fontsize=8, frameon=False)
        quality_fig.suptitle(
            f"Move and score differences | {label}", fontsize=12
        )
        quality_filename = f"quality_{token}.png"
        quality_fig.savefig(
            output_dir / quality_filename, dpi=FIGURE_DPI, bbox_inches="tight"
        )
        plt.close(quality_fig)
        quality_figures.append((quality_filename, label))

    return performance_figures, quality_figures


def write_html(
    output_dir, experiment_dir, performance_figures, quality_figures,
    summary, unmatched_normal, unmatched_rotated,
):
    links = "\n".join(
        f'<li><a href="{html.escape(name)}">{html.escape(name)}</a></li>'
        for name in (
            "configuration_summary.csv", "paired_runs.csv",
            "repetition_quality.csv", "paired_positions.csv",
        )
    )
    definitions = [
        "Rotation off (0) is the normal Weiss move order. Rotation on (1) changes only helper root-move order; the main worker and below-root move ordering stay unchanged.",
        "Runs are paired within each baseline/candidate build by phase, repetition, bucket size, requested depth, worker count, TT size, helper-cap setting, and binary hash. Per-position rows are additionally paired by position.",
        "Wall time is the benchmark's summed time for its 50 positions to reach the requested depth. It is the primary performance measure here. Positive paired improvement means rotated mode took less time: (normal − rotated) / normal × 100.",
        "Where shown, error bars are ±1 sample standard deviation across paired repetitions. They describe run-to-run spread; they are not confidence intervals.",
        "Total-node and main-worker-node changes describe how the search differed. A lower node count is not automatically a better result; compare it with wall time and the requested-depth completion.",
        "Move agreement and CP-score deltas quantify output differences between modes. They do not say which mode is objectively stronger. Mate-score pairs are excluded from CP deltas.",
        "If a time budget left an unmatched run, it is omitted from paired summaries. Normal-only unmatched runs: "
        f"{unmatched_normal}; rotated-only unmatched runs: {unmatched_rotated}.",
    ]
    definitions_html = "\n".join(
        f"<li>{html.escape(item)}</li>" for item in definitions
    )
    phase_rows = summary.groupby(
        ["condition", "phase", "depth", "threads", "hash_mb"], dropna=False
    ).size().reset_index(name="paired configurations")
    phase_table = phase_rows.to_html(index=False, border=0, classes="data-table")
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weiss helper root move-order comparison</title>
<style>
body{{font:16px/1.5 system-ui,-apple-system,sans-serif;max-width:1500px;margin:30px auto;padding:0 24px;color:#172033;background:#f5f7fb}}
h1,h2{{line-height:1.2}}section{{background:#fff;padding:18px 24px;margin:18px 0;border-radius:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:18px}}
figure{{margin:0;background:#fff;padding:10px;border:1px solid #d8deea;border-radius:8px}}
img{{display:block;width:100%;height:auto}}figcaption{{font:14px ui-monospace,monospace;overflow-wrap:anywhere;margin:6px}}
code{{background:#eef1f7;padding:2px 5px;border-radius:4px}}
.data-table{{border-collapse:collapse}}.data-table th,.data-table td{{padding:5px 10px;border-bottom:1px solid #d8deea;text-align:left}}
@media(max-width:650px){{body{{padding:0 10px}}.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>Helper root move-order comparison</h1>
<section>
<p>Paired analysis of the existing Weiss 50-position benchmark with normal helper ordering versus worker-index root-move rotation.</p>
<p><b>Experiment:</b> <code>{html.escape(str(experiment_dir))}</code><br>
<b>Matched configurations:</b> {len(summary)}; performance charts use paired repetitions.</p>
<h2>How to read the results</h2>
<ul>{definitions_html}</ul>
<h2>Available configuration groups</h2>
{phase_table}
</section>
<section><h2>Performance and search work</h2><div class="grid">{''.join(
        f'<figure><a href="{html.escape(name)}"><img src="{html.escape(name)}" alt="{html.escape(label)}"></a>'
        f'<figcaption>{html.escape(label)}</figcaption></figure>'
        for name, label in performance_figures
    )}</div></section>
<section><h2>Move and score differences</h2><div class="grid">{''.join(
        f'<figure><a href="{html.escape(name)}"><img src="{html.escape(name)}" alt="{html.escape(label)}"></a>'
        f'<figcaption>{html.escape(label)}</figcaption></figure>'
        for name, label in quality_figures
    )}</div></section>
<section><h2>Download the analysis tables</h2><ul>{links}</ul></section>
</body>
</html>
"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main():
    args = parse_args()
    experiment_dir = resolve_path(args.experiment)
    output_dir = (
        resolve_path(args.output)
        if args.output
        else (experiment_dir / "analysis-move-order")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    runs, positions = read_data(experiment_dir)
    paired_runs, unmatched_normal, unmatched_rotated = make_paired_runs(runs)
    paired_positions = make_paired_positions(positions)
    repetition_quality = summarize_quality(paired_positions)
    summary = summarize_configurations(paired_runs, repetition_quality)

    save_csv(summary, output_dir / "configuration_summary.csv")
    save_csv(paired_runs, output_dir / "paired_runs.csv")
    save_csv(repetition_quality, output_dir / "repetition_quality.csv")
    save_csv(paired_positions, output_dir / "paired_positions.csv")

    performance_figures, quality_figures = make_figures(summary, output_dir)
    write_html(
        output_dir, experiment_dir, performance_figures, quality_figures,
        summary, unmatched_normal, unmatched_rotated,
    )
    print(f"Analysed {len(paired_runs)} matched benchmark run pairs.")
    print(f"Saved paired CSV summaries and HTML dashboard: {output_dir / 'index.html'}")


if __name__ == "__main__":
    main()
