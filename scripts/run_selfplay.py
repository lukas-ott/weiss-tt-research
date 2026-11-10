#!/usr/bin/env python3
"""Run a paired, color-swapped UCI match between two configured engine builds.

Preview only by default. Pass --run to launch Cute Chess and start games.
"""

import argparse
import csv
import hashlib
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_experiments as experiment_tools

# ======================== EDIT STRENGTH SETTINGS HERE =========================
CUTECHESS_CLI = "cutechess-cli"
EXPERIMENT_SLUG = "baseline-vs-variant-strength"

# Point these at two UCI executables. Keep Threads, Hash and all other relevant
# settings equal unless that setting is itself the treatment being studied.
BASELINE = {
    "name": "Weiss baseline",
    "command": ROOT / "src" / "weiss-baseline",
    "args": [],
    "options": {"Threads": 1, "Hash": 64},
}
VARIANT = {
    "name": "Weiss variant",
    "command": ROOT / "src" / "weiss-variant",
    "args": [],
    "options": {"Threads": 1, "Hash": 64},
}

# Supply a reviewed PGN opening suite. Do not use the tactical speed benchmark.
OPENINGS_PGN = None                 # Example: ROOT / "experiments" / "openings.pgn"
OPENING_PLIES = 16
TIME_CONTROL = "40/60+0.6"          # 40 moves in 60 s, then 0.6 s increment.
MAX_GAMES = 1000                    # Must be even so the maximum preserves pairs.
CONCURRENCY = 1                     # One game at a time; avoid CPU oversubscription.
SPRT = {"elo0": 0, "elo1": 5, "alpha": 0.05, "beta": 0.05}
# ===============================================================================

HEADER = re.compile(r'^\[([A-Za-z0-9_]+)\s+"((?:[^"\\]|\\.)*)"\]\s*$')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def absolute_engine(engine):
    command = Path(engine["command"])
    return command if command.is_absolute() else (ROOT / command).resolve()


def build_command(output_dir, openings_path=None, engine_binary_paths=None):
    openings_path = Path(openings_path) if openings_path else Path("<set-reviewed-openings.pgn>")
    if not openings_path.is_absolute() and str(openings_path) != "<set-reviewed-openings.pgn>":
        openings_path = (ROOT / openings_path).resolve()
    if engine_binary_paths is None:
        engine_binary_paths = [
            Path(output_dir) / "bin" / "variant" / absolute_engine(VARIANT).name,
            Path(output_dir) / "bin" / "baseline" / absolute_engine(BASELINE).name,
        ]
    command = [CUTECHESS_CLI]
    # Cute Chess SPRT tests player 1; put the treatment (variant) first.
    for engine, binary in zip((VARIANT, BASELINE), engine_binary_paths):
        command += ["-engine", f"name={engine['name']}", "proto=uci",
                    f"cmd={binary}", f"dir={engine.get('working_dir', ROOT)}"]
        command += [f"arg={arg}" for arg in engine.get("args", [])]
        command += [f"option.{key}={value}" for key, value in engine.get("options", {}).items()]
    command += ["-each", "proto=uci", f"tc={TIME_CONTROL}",
                "-concurrency", str(CONCURRENCY),
                "-rounds", str(MAX_GAMES), "-repeat",
                "-openings", f"file={openings_path}", "format=pgn", "order=sequential",
                f"plies={OPENING_PLIES}",
                "-pgnout", str(Path(output_dir) / "games.pgn")]
    if SPRT is not None:
        command += ["-sprt", *(f"{key}={value}" for key, value in SPRT.items())]
    return command


def validate_settings(require_files):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", EXPERIMENT_SLUG):
        raise ValueError("EXPERIMENT_SLUG must use lowercase letters, numbers and hyphens")
    if BASELINE["name"] == VARIANT["name"]:
        raise ValueError("Baseline and variant need distinct engine names")
    if type(MAX_GAMES) is not int or MAX_GAMES < 2 or MAX_GAMES % 2:
        raise ValueError("MAX_GAMES must be an even integer of at least 2")
    if type(OPENING_PLIES) is not int or OPENING_PLIES < 0:
        raise ValueError("OPENING_PLIES must be a nonnegative integer")
    if type(CONCURRENCY) is not int or CONCURRENCY != 1:
        raise ValueError("Use CONCURRENCY = 1 for this controlled match setup")
    if not TIME_CONTROL.strip():
        raise ValueError("TIME_CONTROL must be set")
    for engine in (BASELINE, VARIANT):
        if not engine.get("name") or not engine.get("command"):
            raise ValueError("Each engine needs a name and executable command")
        if not isinstance(engine.get("options", {}), dict):
            raise ValueError("Engine options must be a dictionary")
    if SPRT is not None:
        required = {"elo0", "elo1", "alpha", "beta"}
        if set(SPRT) != required or SPRT["elo0"] >= SPRT["elo1"]:
            raise ValueError("SPRT must set elo0 < elo1, alpha and beta")
        if any(not 0 < float(SPRT[key]) < 1 for key in ("alpha", "beta")):
            raise ValueError("SPRT alpha and beta must be between zero and one")
    if not require_files:
        return None
    if OPENINGS_PGN is None:
        raise ValueError("Set OPENINGS_PGN to a reviewed PGN opening suite before using --run")
    openings = Path(OPENINGS_PGN)
    if not openings.is_absolute():
        openings = (ROOT / openings).resolve()
    if not openings.is_file() or openings.stat().st_size == 0:
        raise ValueError(f"Opening PGN does not exist or is empty: {openings}")
    cutechess = shutil.which(CUTECHESS_CLI)
    if cutechess is None:
        raise ValueError(f"Cannot find {CUTECHESS_CLI!r}; install Cute Chess CLI on the run machine")
    engine_paths = []
    for engine in (BASELINE, VARIANT):
        binary = absolute_engine(engine)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"Engine binary is missing or not executable: {binary}")
        engine_paths.append(binary)
    return openings.resolve(), cutechess, engine_paths


def parse_pgn_headers(path):
    """Read result headers using only the standard library; moves are not parsed."""
    games = []
    tags = {}

    def finish():
        nonlocal tags
        if tags:
            games.append(tags)
            tags = {}

    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = HEADER.fullmatch(line.strip())
        if match:
            if "Result" in tags:
                finish()
            value = match.group(2).replace(r'\"', '"').replace(r"\\", "\\")
            tags[match.group(1)] = value
        elif not line.strip() and "Result" in tags:
            finish()
    finish()
    return games


def summarize_games(games, output_dir):
    game_path = Path(output_dir) / "game-results.csv"
    fields = ["game", "white", "black", "result", "variant_score", "variant_outcome"]
    rows = []
    variant_name = VARIANT["name"]
    baseline_name = BASELINE["name"]
    wins = draws = losses = unfinished = 0
    unclassified = 0
    points = 0.0
    for number, tags in enumerate(games, 1):
        white, black = tags.get("White", ""), tags.get("Black", "")
        result = tags.get("Result", "*")
        score, outcome = "", "unfinished"
        if {white, black} != {baseline_name, variant_name}:
            outcome = "unclassified"
            unclassified += 1
        elif result == "1/2-1/2":
            score, outcome = 0.5, "draw"
            draws += 1
            points += 0.5
        elif result in ("1-0", "0-1"):
            variant_won = (result == "1-0" and white == variant_name) or (
                result == "0-1" and black == variant_name)
            score = 1 if variant_won else 0
            outcome = "win" if variant_won else "loss"
            wins += int(variant_won)
            losses += int(not variant_won)
            points += score
        else:
            unfinished += 1
        rows.append({"game": number, "white": white, "black": black,
                     "result": result, "variant_score": score,
                     "variant_outcome": outcome})
    with game_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    complete = wins + draws + losses
    score_pct = (100 * points / complete) if complete else ""
    summary = {
        "baseline": BASELINE["name"], "variant": variant_name,
        "games_in_pgn": len(games), "completed_games": complete,
        "unfinished_games": unfinished, "unclassified_games": unclassified,
        "variant_wins": wins,
        "draws": draws, "variant_losses": losses,
        "variant_points": points, "variant_score_pct": score_pct,
    }
    with (Path(output_dir) / "summary.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary))
        writer.writeheader()
        writer.writerow(summary)
    return summary


def make_report(output_dir, source_commit, source_status, binary_records,
                opening_digest, state, results, conclusion):
    settings = {
        "Baseline": BASELINE["name"],
        "Variant": VARIANT["name"],
        "Time control": TIME_CONTROL,
        "Threads / Hash": f"baseline {BASELINE['options']}; variant {VARIANT['options']}",
        "Opening PGN": OPENINGS_PGN if OPENINGS_PGN is not None else "not configured",
        "Opening SHA-256": opening_digest or "not available",
        "Opening plies": OPENING_PLIES,
        "Maximum games": MAX_GAMES,
        "Paired colors": "Cute Chess -repeat; same opening played with colors swapped",
        "Concurrency": CONCURRENCY,
        "SPRT": (f"{SPRT}; variant is first-listed player (P1)" if SPRT is not None
                 else "disabled"),
    }
    controls = [
        "One game runs at a time; both engines use the same time control.",
        "Each opening is paired with colors swapped unless an SPRT decision stops the match between games.",
        "Use the same engine options for both sides except for the one treatment under study.",
        "The opening file, order and selected opening plies stay fixed across the comparison.",
    ]
    limitations = [
        "Stockfish move overlap is not used as a strength result; game outcomes are the measure.",
        "Playing strength depends on the opening suite, time control, engine options and tested hardware.",
        "The match log is authoritative for SPRT stopping; this runner reports results but does not infer causality.",
    ]
    experiment_tools.write_experiment_md(
        output_dir, title="Weiss self-play strength comparison",
        question="Does the configured variant score differently from the baseline in paired games?",
        hypothesis="The variant is expected to score higher if the change improves playing strength under the fixed match protocol; a speed or node-count change alone does not imply this.",
        settings=settings, controls=controls, source_commit=source_commit,
        source_status=source_status, runner_file=Path(__file__).name,
        binaries=binary_records, status=state, results=results,
        conclusion=conclusion, limitations=limitations)


def capture_source(output_dir):
    commit = experiment_tools.capture(["git", "rev-parse", "HEAD"], required=True)
    status = experiment_tools.capture(["git", "status", "--short"], required=True)
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    (Path(output_dir) / "source.patch").write_bytes(patch)
    shutil.copyfile(__file__, Path(output_dir) / "runner.py")
    shutil.copyfile(ROOT / "scripts" / "run_experiments.py",
                    Path(output_dir) / "run_experiments.py")
    return commit, status


def run_match(output_dir, openings, cutechess, engine_paths):
    output_dir.mkdir(parents=True, exist_ok=False)
    status_path = output_dir / "status.txt"
    status_path.write_text("preparing\n", encoding="utf-8")
    commit, source_status = capture_source(output_dir)
    binary_dir = output_dir / "bin"
    binary_paths = {}
    for role, source in zip(("baseline", "variant"), engine_paths):
        destination = binary_dir / role / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        binary_paths[role] = destination
    recorded_openings = output_dir / "openings.pgn"
    shutil.copy2(openings, recorded_openings)
    binary_records = [(engine["name"], sha256(binary))
                      for engine, binary in ((BASELINE, binary_paths["baseline"]),
                                             (VARIANT, binary_paths["variant"]))]
    opening_digest = sha256(openings)
    version = subprocess.run([cutechess, "--version"], cwd=ROOT,
                             capture_output=True, text=True, check=False)
    command = build_command(output_dir, recorded_openings,
                            [binary_paths["variant"], binary_paths["baseline"]])
    (output_dir / "command.txt").write_text(shlex.join(command) + "\n", encoding="utf-8")
    run_info = [
        f"started_utc: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"hostname: {os.uname().nodename if hasattr(os, 'uname') else 'unknown'}",
        f"platform: {platform.platform()}", f"machine: {platform.machine()}",
        f"python: {sys.version}",
        f"allowed_cpus: {sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else 'unavailable'}",
        "placement: inherited from invoking shell/scheduler; no per-engine pinning",
        f"source_commit: {commit}", f"source_status_before_run:\n{source_status or '(clean)'}",
        f"cutechess_cli: {cutechess}",
        f"cutechess_version: {(version.stdout + version.stderr).strip() or 'version command unavailable'}",
        f"opening_pgn: {openings}", f"recorded_opening_pgn: {recorded_openings}",
        f"opening_sha256: {opening_digest}",
        f"baseline_binary: {engine_paths[0]}", f"baseline_copy: {binary_paths['baseline']}",
        f"baseline_sha256: {binary_records[0][1]}",
        f"variant_binary: {engine_paths[1]}", f"variant_copy: {binary_paths['variant']}",
        f"variant_sha256: {binary_records[1][1]}",
        f"command: {shlex.join(command)}",
    ]
    for metadata_command in (["lscpu"], ["numactl", "--show"]):
        run_info.append(f"\n$ {shlex.join(metadata_command)}\n"
                        + experiment_tools.capture(metadata_command))
    (output_dir / "run-info.txt").write_text("\n".join(run_info) + "\n", encoding="utf-8")
    make_report(output_dir, commit, source_status, binary_records, opening_digest,
                "running", "Match started; final score is pending.",
                "Pending match completion and manual interpretation.")
    status_path.write_text("running\n", encoding="utf-8")
    print(f"Results: {output_dir}", flush=True)
    process = None
    launch_error = None
    try:
        with (output_dir / "cutechess.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
            return_code = process.wait()
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        state = "interrupted"
        return_code = 130
    except OSError as error:
        launch_error = str(error)
        return_code = 127
    games = parse_pgn_headers(output_dir / "games.pgn") if (output_dir / "games.pgn").exists() else []
    summary = summarize_games(games, output_dir)
    if return_code == 0:
        state = "complete"
    elif return_code != 130:
        state = f"failed (Cute Chess exit code {return_code}; {launch_error})" if launch_error else (
            f"failed (Cute Chess exit code {return_code})")
    score = summary["variant_score_pct"]
    result_text = (f"Completed games: {summary['completed_games']}; unfinished PGN records: "
                   f"{summary['unfinished_games']}; variant W/D/L: "
                   f"{summary['variant_wins']}/{summary['draws']}/{summary['variant_losses']}; "
                   f"variant score: {score}%; unclassified games: {summary['unclassified_games']}" if score != "" else
                   f"No completed games; PGN records: {summary['games_in_pgn']}.")
    conclusion = ("Review cutechess.log for the SPRT decision and game summary.csv/PGN for outcomes. "
                  "The runner does not infer why a score changed.")
    status_path.write_text(f"{state}\n", encoding="utf-8")
    make_report(output_dir, commit, source_status, binary_records, opening_digest,
                state, result_text, conclusion)
    print(f"{state}: {result_text}", flush=True)
    return return_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="launch Cute Chess; without this option the runner only previews")
    args = parser.parse_args()
    try:
        validated = validate_settings(require_files=args.run)
        output_dir = experiment_tools.experiment_directory(
            ROOT / "results" / "experiments", EXPERIMENT_SLUG)
        openings = None
        cutechess = CUTECHESS_CLI
        engine_paths = [absolute_engine(BASELINE), absolute_engine(VARIANT)]
        if args.run:
            openings, cutechess, engine_paths = validated
        command = build_command(output_dir, openings or OPENINGS_PGN)
        if not args.run:
            print("Preview only. No engine or game was started.")
            print(f"Output directory: {output_dir}")
            if OPENINGS_PGN is None:
                print("Set OPENINGS_PGN to a reviewed opening PGN before using --run.")
            print(shlex.join(command))
            print("Pass --run to launch the match after setting engine paths and openings.")
            return 0
        return run_match(output_dir, openings, cutechess, engine_paths)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Self-play match not started: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
