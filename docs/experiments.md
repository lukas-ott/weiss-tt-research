# Running experiments

The experiment runner uses Weiss's existing ordered suite of 50 positions. It
builds one executable per bucket size and starts a fresh process for each complete
benchmark. Runs execute sequentially. There are no third-party Python dependencies.

## Prepare the server checkout

First commit and push the intended engine/runner version from your Mac; cloning
only retrieves committed, pushed files. No commit or push is performed by the runner.

After connecting to `kiloampere` through `hulk`, clone on **kiloampere**, not on the
gateway:

```bash
hostname  # should print kiloampere
git clone https://github.com/lukas-ott/weiss-tt-research.git
cd weiss-tt-research
```

For a branch that has been pushed, switch to it with `git switch BRANCH_NAME`.
For an existing checkout, update the intended branch instead of cloning again.
Required tools are Python 3.10+, Git, Make, and a C compiler supporting the
existing GNU C11/native/LTO build. The settings default to GCC. No package is
installed and no administrator settings are changed by the runner.

Your transcript reports 192 physical Ampere cores, no SMT, four NUMA nodes and
about 1 TiB RAM. Confirm how server time/cores are allocated before a large run;
an idle snapshot or zero logged-in users does not establish exclusive access.
If a scheduler is required, invoke the runner within the assigned job.

## Edit the experiment

Open `scripts/run_experiments.py` and edit the marked settings block:

```python
BUCKET_SIZES = [2]           # allowed: 1, 2, 4, 8
DEPTHS = [12]
THREAD_COUNTS = [1, 2, 4, 8]
HASH_SIZES_MB = [64]
REPETITIONS = 5
ORDER_SEED = 2026
CC = "gcc"
TIMEOUT_SECONDS = None
```

Every combination is run once per repetition. These example settings mean 20
complete benchmarks, or 1,000 position searches. They are an editable example,
not a calibrated full-server workload. For all physical core counts, one possible
list is `[1, 2, 4, 8, 16, 32, 48, 64, 96, 128, 192]`. Only use counts permitted by
your allocation. The runner rejects a count exceeding its Linux CPU-affinity mask;
that check does not establish exclusivity or detect every scheduler CPU quota.

Bucket size is compiled in; depth, total search-worker count and TT MiB are runtime
arguments. Linux TT sizes must be even and at least 2 MiB. `TIMEOUT_SECONDS`, when
set, limits an entire 50-position benchmark process, not an individual position;
a timed-out run is incomplete and is not added to the CSV results.

## Helper root move-order experiment

`scripts/run_move_order.py` compares the existing search order with a helper-only
cyclic rotation. It runs one configurable parameter grid; there is no separate
high-repetition baseline phase. Edit `DEPTHS`, `THREAD_COUNTS`, `HASH_SIZES_MB`
and `REPETITIONS` at the top of the script. For example, set `DEPTHS = [15]` for
one depth or use `list(range(15, 24))` for depths 15 through 23. Preview the plan
without building or searching:

```bash
python3 scripts/run_move_order.py --dry-run
```

When deliberately ready to run it:

```bash
python3 scripts/run_move_order.py
```

For every selected configuration and repetition, the runner requests both
`rotate_root_moves=0` (normal-order control) and `=1` (rotation), randomized within
the repetition and using the same executable. The default grid is depths 15–23,
1/32/128/192 workers, 2/64/512 MiB TT, and five paired repetitions. Helpers are
uncapped in both modes (`cap_helpers=0`), so they may search beyond the requested
benchmark depth. The 6.5-hour default includes both move-order modes and may stop
before every grid combination finishes. Narrow `DEPTHS`, `THREAD_COUNTS`, or
`HASH_SIZES_MB` if you want fewer configurations; the normal-order control remains
in every selected configuration.
The latter rotates the complete normally ordered legal root-move sequence of each
helper by `worker_index % root_move_count` at each root MovePicker setup. Worker 0
keeps the normal order, and all deeper move pickers remain unchanged. Rotation may
move the TT move away from first place in a helper; that is part of the treatment.
The root move list is generated in that helper's current TT/capture/killer/history
order before rotation, rather than using the separate `RootMove` bookkeeping array.

Rotation has a runtime benchmark argument and is recorded in `BENCH_CONFIG`,
`plan.csv`, `runs.csv` and `positions.csv`. The standard scaling runner leaves it
off unless `ROOT_MOVE_ROTATIONS` is deliberately changed. Existing CSVs without the
column are interpreted as unrotated. The dedicated runner writes to a
`results/experiments/YYYYMMDD_helper-root-move-order/` (with a numeric suffix if
needed). Each run includes an `experiment.md` record. It launches searches only
when invoked without `--dry-run`.

Combinations are shuffled separately within each repetition using `ORDER_SEED`.
The exact order is saved before building. A fixed seed reproduces ordering; it
does not make the parallel search deterministic. No extra warm-up search runs are
inserted. The original non-PGO `basic` build and its optimization flags are used
for every variant. Compilations finish before measured runs begin.

Preview without building, running searches, or creating output files:

```bash
python3 scripts/run_experiments.py --dry-run
```

When you deliberately want to start the configured experiment:

```bash
python3 scripts/run_experiments.py
```

Do not edit the engine sources or start another runner in the same checkout while
it is building/running. Each invocation creates a new dated, descriptive output
folder. The slug is editable in the corresponding runner settings block. The runner
has no automatic resume or background-job management.

## SSH disconnects and placement

Where interactive jobs are allowed, an existing `tmux` installation can keep the
session alive after an SSH disconnect:

```bash
tmux new -s weiss
python3 scripts/run_experiments.py
```

Detach using `Ctrl-b`, then `d`. After reconnecting, use `tmux attach -t weiss`.
See the [tmux getting-started guide](https://github.com/tmux/tmux/wiki/Getting-Started).
A runner started directly in an ordinary SSH terminal is not protected from a
session disconnect. Follow the scheduler's rules if interactive jobs are not allowed.

The runner inherits the invoking process's CPU affinity and NUMA policy. It does
not reserve CPUs, pin individual workers, force a memory policy, or alter CPU
frequency. On this four-node server, record and hold the placement policy constant
for a given comparison. If you and the administrator choose a policy, launch the
runner under it, for example using the options documented in the
[numactl manual](https://github.com/numactl/numactl/blob/master/numactl.8).
The runner records allowed CPUs and available `numactl` information; unavailable
metadata tools are noted rather than installed. Recorded policy does not prove
that the server was otherwise idle.

## Output and validation

Each invocation creates `results/experiments/YYYYMMDD_<experiment-slug>/` (adding
`_02`, `_03`, etc. if that name already exists):

| File | Contents |
|---|---|
| `plan.csv` | Every requested combination, repetition and execution order |
| `runs.csv` | One row per successfully completed/validated benchmark; summed clocks and nodes |
| `positions.csv` | 50 rows per validated benchmark; clocks, nodes, move and searched score |
| `raw/run-NNNN.log` | Unmodified engine output, including partial output after a failure |
| `bin/weiss-bN` | Exact executable used for bucket size N |
| `build-bN.log`, `build-commands.txt` | Compiler output and exact build commands |
| `run-info.txt` | Git revision/status, system/compiler metadata, affinity, available NUMA information |
| `source.patch`, `runner.py` | Tracked changes relative to the revision, plus the exact runner/settings |
| `experiment.md` | Question, hypothesis, settings, controls, source/build IDs, results and limitations |
| `status.txt` | Building, current run, complete, failed, or interrupted |

CSV rows include the run number, repetition, parameters (including helper root
move-order mode), binary SHA-256 and raw-log
path. Moves use UCI notation. `score_type` is `cp` or `mate`; `score` means
centipawns or signed moves to mate from the side-to-move perspective. `raw_score`
is the internal searched score. All clocks are in nanoseconds; `runs.csv` also
retains `elapsed_ms` and NPS for compatibility/readability.

Before accepting a run, the runner checks the effective parameters and table
layout, exactly positions 1..50 at the requested depth, basic move/score formats,
and equality of position sums with the clock/node totals. Successful process
exit alone is not sufficient. On failure it stops, preserving earlier completed
rows, the failing raw log and a non-complete status. `Ctrl-C` also preserves
completed rows. A killed process may leave status as `running`; that folder is
not a completed experiment. No partial run is silently converted into valid data.

Generated CSVs, binaries, raw logs and source snapshots are ignored by Git. The
short `experiment.md` is retained by Git; archive/copy the full directory to
preserve measurements and binaries. Pushing the repository does not back up the
full experiment. Keep the recorded source revision available. Untracked build
inputs under `src/` must be added to Git before running so the source patch can
capture their contents; `src/notes.md` is documentation and does not block a run.
The runner writes descriptive results, but leaves the interpretation pending.
After analyzing the data, update `experiment.md` with the conclusion, uncertainty
and limitations before treating the record as complete.

The descriptive record for the earlier five-repetition Mac depth-15 pilot is
[`20261028_depth15-thread-scaling-macos/experiment.md`](../results/experiments/20261028_depth15-thread-scaling-macos/experiment.md).
Its larger run bundle remains local and ignored.

## What to compare

Use elapsed time (`wall_ns`) for time-to-depth speedup. Process CPU time (`cpu_ns`)
sums CPU consumed across workers and measures resource use. `main_nodes` separates
the main worker's work from aggregate `nodes`. The run clocks are sums of the
per-position search intervals, not the wall time of the whole Python invocation.

Search preparation and worker startup/joining are timed. TT allocation, initial
zeroing, inter-position clearing, FEN parsing and printing are excluded. The TT
starts empty for each position; thread histories/caches persist across the ordered
positions as in the original benchmark. Multi-thread results can vary. Keep these
rules unchanged across comparisons and collect new timing baselines, since older
POC timings included thinking-output overhead. No Stockfish reference analysis,
pruning changes or TT synchronization redesign is part of this runner.

Choose depth so the workload is long enough to study search rather than mostly
worker startup. No suitable depth or runtime budget has yet been calibrated on
kiloampere. Use repetitions and inspect timing variation before making a scaling
claim; the runner produces data rather than automatically claiming significance.

## Analyze results with pandas

Install the analysis dependency in your Python environment (Python 3.11+):

```bash
python -m pip install -r requirements-analysis.txt
```

In each script, edit `EXPERIMENT_DIR` near the top to point to one completed
experiment folder. Then run these commands from the repository root:

```bash
python3 scripts/analyze_runs.py
python3 scripts/analyze_positions.py
```

Both scripts print their summaries and write CSV files inside the experiment
folder. Rerunning replaces the summaries; the original input files stay unchanged.

- `run_summary.csv`: repetition count; wall runtime mean, median, minimum, maximum,
  sample variance and standard deviation; mean CPU time, total nodes, main-worker
  nodes and throughput. Throughput is the arithmetic mean of each run's nodes
  divided by its wall runtime.
- `position_summary.csv`: for each position and configuration, move frequencies,
  distinct move count, centipawn and mate counts, and centipawn score mean, sample
  variance and standard deviation. Mate results count toward move frequencies but
  are excluded from score statistics, including when a group mixes score types.

Configurations are separated by bucket size, depth, threads, TT MiB, helper cap,
root move-order mode and executable SHA-256. Positions are also grouped by position
ID. Do not combine experiment
folders from different machines or placement settings.

Wall runtime measures time to depth; CPU time accumulates across workers. Times
are in seconds, runtime variance in seconds squared, evaluation in centipawns and
score variance in centipawns squared. Sample variance divides by `n-1`; variance
and standard deviation are blank when fewer than two relevant observations exist.
For example, scores 10 and 30 cp have mean 20 cp and sample variance 200 cp squared.
Move variability does not establish move correctness or playing strength.

The experiment runners use only the Python standard library. Pandas is needed for
the summary/comparison scripts; Matplotlib is additionally needed for
`scripts/visualize_experiments.py`.

## Playing-strength self-play

Benchmark time-to-depth and playing strength answer different questions. Use
[`scripts/run_selfplay.py`](../scripts/run_selfplay.py) for a separate match
between two UCI engine builds. It uses no Python packages, but requires the
[Cute Chess CLI](https://github.com/cutechess/cutechess) executable on the machine
where the match runs.

Edit the settings block in the script:

- Point `BASELINE` and `VARIANT` to the two engine binaries. Keep `Threads`,
  `Hash`, and other relevant UCI options equal unless that setting is the
  treatment.
- Set `OPENINGS_PGN` to a reviewed opening PGN suite. The 50 tactical benchmark
  positions are not a playing-strength opening suite.
- Keep `OPENING_PLIES`, `TIME_CONTROL`, `MAX_GAMES`, and `SPRT` fixed when
  comparing variants. The checked-in values are starting settings, not calibrated
  thesis results.

Preview the command (no games start):

```bash
python3 scripts/run_selfplay.py
```

After configuring both binaries and the opening file, explicitly start the match:

```bash
python3 scripts/run_selfplay.py --run
```

The protocol uses one game at a time, pairs openings with colors swapped, saves
`games.pgn`, preserves the complete `cutechess.log`, and requests an SPRT with
editable Elo bounds. The variant is listed first because Cute Chess's SPRT
hypotheses are oriented to player 1. Cute Chess can stop after an SPRT decision or
at the game limit. Its log is authoritative for the decision; inspect `summary.csv`,
`game-results.csv` and the PGN before interpreting the result. Each output folder
contains its own `experiment.md`, binary/opening hashes and exact command.

See the [Cute Chess CLI manual](https://github.com/cutechess/cutechess/blob/master/docs/cutechess-cli.6)
for the documented `-repeat`, `-openings`, `-rounds`, `-pgnout` and `-sprt` options.

## Compare moves and scores with Stockfish

The saved [Stockfish 19 reference](../results/stockfish19-reference-20261029/README.md)
has five moves per benchmark position at nominal depth 28. To reproduce the
included server comparison, use the same optional pandas environment:

```bash
python3 scripts/compare_stockfish.py
```

Edit `EXPERIMENT_DIR`, `REFERENCE_DIR`, `OUTPUT_DIR` and `NEAR_BEST_CP` in the marked
settings block for another batch. The default input is the original kiloampere
batch under
`results/experiments/20261028_depth15-thread-scaling-kiloampere`; it must be
available locally, or you must select its location. Raw server inputs are not uploaded to
Git. Outputs are replaced on rerun; input CSVs are preserved.

The script saves per-observation, per-position, per-repetition and per-configuration
CSVs, a missing-move list, and a comparison restricted to positions scored across
all configurations. Configurations/build hashes stay separate. Do not combine
machines or experiments with different placement policies in one input folder.
Position IDs must refer to the same original benchmark FENs; the script checks the
saved reference against `src/tests.c` but cannot infer historical FENs from a new
Weiss CSV that has no FEN column.

Cross-engine score differences are numeric discrepancies: Weiss and Stockfish do
not use identical evaluation scales. Move quality is estimated separately using
Stockfish's score for its best move minus its score for the chosen Weiss move.
Moves absent from the top five remain unscored. Mate distances are excluded from
centipawn arithmetic. See the [comparison report](../results/stockfish-comparison/kiloampere-20261028/README.md)
for formulas, denominators, findings and limitations.

## Test the runner without running chess searches

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests use synthetic logs/data and fake build/engine commands. They do not compile
Weiss, connect to a server, or launch real benchmark searches. Runner tests use only
the standard library. Stockfish-comparison tests use pandas and are skipped if it
is not installed.
