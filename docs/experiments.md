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
it is building/running. Each invocation creates a new timestamped output folder.
The runner has no automatic resume or background-job management.

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

Each invocation creates `results/experiments/<UTC timestamp>/`:

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
| `status.txt` | Building, current run, complete, failed, or interrupted |

CSV rows include the run number, repetition, parameters, binary SHA-256 and raw-log
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

The entire output directory is ignored by Git, including binaries and raw logs.
Archive/copy the whole directory separately; pushing the repository does not back
up experimental results. Keep the recorded source revision available. Untracked
source files under `src/` must be added to Git before running so the source patch
can capture their contents. Tracked local edits are recorded; a committed version
is easier to reproduce.

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

## Test the runner without running chess searches

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests use synthetic logs and fake build/engine commands. They do not compile Weiss,
connect to a server, or launch real benchmark searches.
