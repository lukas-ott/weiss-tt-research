# Running Weiss experiments

There is one experiment launcher: `scripts/run_experiments.py`. It runs the
existing ordered 50-position benchmark, compares every configuration to a pinned
baseline build on exactly the same workload, and ends with a short self-play
smoke check.

## Run it on kiloampere

Build and run from the server checkout, inside the CPU allocation you intend to
use. The script needs Python 3.10+, Git, Make, a C compiler and Cute Chess CLI for
the final smoke match. With the default `SELFPLAY_REQUIRED = True`, it checks
that Cute Chess is on `PATH` before starting any searches. No Python packages are
needed by the runner.

```bash
python3 scripts/run_experiments.py --dry-run
python3 scripts/run_experiments.py
```

Use `tmux` if the server allows long interactive jobs and you may disconnect:

```bash
tmux new -s weiss
python3 scripts/run_experiments.py
```

Detach with `Ctrl-b`, then `d`; reconnect with `tmux attach -t weiss`. Follow any
server scheduler or CPU-allocation rules. The runner inherits affinity and NUMA
policy; it does not reserve or pin CPUs.

## Edit the experiment

The `WEISS_DEFAULT` dictionary is the automatic control: two entries per bucket,
benchmark depth 14, one worker and 32 MiB Hash. It is included every time; do not
repeat it in the experiment list. For a study, edit only `EXTRA_RUNS`. Each entry
has a descriptive name and only the settings it changes. The unspecified settings
are copied from `WEISS_DEFAULT`:

```python
EXTRA_RUNS = [
    {"name": "threads-2", "threads": 2},
    {"name": "threads-32", "threads": 32},
    {"name": "hash-64", "hash_mb": 64},
]
```

This means three separate extra configurations plus the automatic default; it
does not create every combination of the listed values. Prefer changing one
setting per extra when you want to attribute an effect to that setting. Use a
named multi-setting extra only when you intend to study those settings together.
An extra that duplicates the default or another configuration is rejected.

For the default and every extra, and for every repetition, the runner executes
two fresh benchmark processes: the pinned baseline source and the current
checkout. They receive the same depth, thread count, TT size, bucket size and
benchmark-only switches. The same ordered 50 positions are searched, one process
at a time. Their order within each pair is randomized with `ORDER_SEED`. Each
`comparison_id` links the pair.
This is an equal-work source comparison; when the current C source is identical
to the reference, the two builds are an implementation check and repeatability
control rather than a search change.

The fixed reference is commit
`c55272499a460acdb7bd057ad55d495b65faaefd`. It is checked out temporarily in a
detached Git worktree, built, and removed before searches start. Its executable
and hash are retained in the output. Change `BASELINE_GIT_REF` only when you
intentionally reset the thesis control, and record why in the experiment record.

`TIME_BUDGET_HOURS` includes builds, benchmark processes and self-play. A
benchmark timeout applies to one complete 50-position process. Completed runs
remain in the CSVs if the budget expires; incomplete logs are not counted as
completed measurements.

## What the clocks mean

`wall_ns` is elapsed wall-clock time for the 50 positions: this is the primary
time-to-depth metric. `cpu_ns` sums CPU time consumed across workers. `nodes` is
aggregate nodes across all workers; `main_nodes` isolates the main worker. More
nodes can mean extra parallel work without faster completion, so do not treat
throughput as the experiment's primary outcome.

TT allocation and clearing, FEN parsing, and console printing are excluded from
the engine's per-position clocks. Search preparation plus worker startup/joining
is included. The TT starts empty for each position; thread histories/caches
persist across the fixed position order. These rules are the same in each pair.

## Self-play at the end

The runner compares the baseline and current builds using Weiss default UCI
settings (one thread, Hash 32 MiB) for four
color-swapped games at `1+0.1`, starting from the normal chess position. It runs
after all search benchmarks so that it cannot affect their timings. The PGN,
Cute Chess log, command and game-result CSV are saved with the run.

Four games are only a smoke test for UCI compatibility, legal play and obvious
crashes. Their score is not an Elo estimate and should not be cited as evidence
of playing strength. A strength experiment needs a reviewed opening suite, a
longer time control, many more paired games and a statistical stopping rule.

## Output files

Each run is saved to `results/experiments/YYYYMMDD_<slug>/`:

| File | Contents |
|---|---|
| `experiment.md` | Question, hypothesis, settings, control protocol, results and limitations |
| `plan.csv` | Exact execution order and paired baseline/candidate IDs |
| `runs.csv` | One row for every completed 50-position process |
| `positions.csv` | Per-position clocks, nodes, move and score |
| `comparisons.csv` | Paired wall-time ratio and work metrics; >1 means candidate was faster |
| `position-comparisons.csv` | Paired per-position clocks, moves, CP score changes and move agreement |
| `raw/` | Complete engine output logs |
| `bin/baseline/`, `bin/candidate/` | Exact binaries used, including hashes in the CSVs |
| `run-info.txt`, `source.patch` | Machine/compiler details and current source changes |
| `selfplay-*` | Four-game smoke match files and explicit status |

`wall_ns` is not whole-runner wall time. `process_wall_ns` separately includes
benchmark-process overhead. Keep the full output directory outside Git with the
rest of the generated data; each concise `experiment.md` is retained in Git.

## Analysis

For quick pandas summaries, point `EXPERIMENT_DIR` near the top of
`scripts/analyze_runs.py` or `scripts/analyze_positions.py` at the generated
folder. Both summaries keep baseline and candidate separate. The paired runtime
ratios are already in `comparisons.csv`. For HTML charts, configure
`EXPERIMENT_DIRS` in `scripts/visualize_experiments.py` and run:

```bash
python3 scripts/visualize_experiments.py
```

The analysis scripts use pandas; the visualizer also uses Matplotlib. Install
those in the local analysis environment using `requirements-analysis.txt`.
