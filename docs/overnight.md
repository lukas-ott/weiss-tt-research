# Overnight helper-depth and TT-size study

This study runs **on kiloampere after you SSH into it**. The script neither connects
to the server nor starts any remote job from your laptop. It needs Python 3.10+,
Git, Make and GCC; no pandas, pip, virtual environment or root access is needed.
An available `tmux` session protects the running job from an SSH disconnect.

## Protocol

The existing ordered suite of 50 positions and two entries per bucket stay fixed.
Both helper modes use the same binary within each batch:

- `cap_helpers=0`: original search; helpers can continue beyond the requested iteration.
- `cap_helpers=1`: every helper returns after finishing the requested iteration.
  This caps **nominal iterative-deepening depth**, not actual branch length or
  quiescence depth, and does not enforce lockstep with the main worker's progress.

| Phase | Depth | Threads | TT MiB | Helper modes | Repetitions |
|---|---|---|---|---|---|
| Baseline and cap comparison | 15 | 1, 2, 4, 8, 16, 32, 64, 128, 192 | 64 | 0 and 1 | 30 each |
| Depth/hash sweep | 15, then 16, 17, ... while feasible | 1, 32, 128, 192 | 2, 64, 512 | 0 and 1 | 5 each |

The first phase contains **540 complete benchmarks / 27,000 position searches**.
It provides six times the previous five repetitions per configuration, plus the
192-thread point and the capped control. The one-thread configurations are useful
controls: the cap should not affect their moves, scores or searched node counts.
Timing still varies. Collect the new uncapped baseline with this same instrumented
build; do not silently pool old binaries or different placement policies.

For each deeper level, a one-repetition pilot measures all 24 configurations.
Pilots have their own folders and are excluded from combined measurement CSVs.
The script estimates a five-repetition batch as 1.5 times the pilot's elapsed
wall time times five, with a ten-minute reserve. Pilot time includes compilation,
metadata collection, all TT clearing, process startup, search and output.
Pilot wall time is itself capped so calibration cannot consume the rest of the
night: it gets at most `remaining_minus_reserve / (1 + 1.5 * 5)`.

If a pilot times out or the estimated measured batch will not fit, the sweep
stops. Remaining usable time adds complete repetitions of the **last fully
measured depth**, using the same 24 configurations. The requested ceiling is 30;
the highest completed depth cannot be predicted from the old depth-15 results.
The estimate is a scheduling rule, not a guarantee that the next batch finishes.
The six-and-a-half-hour wall-time deadline also bounds subprocess execution.

Each whole 50-position process has a five-minute watchdog. Timeout logs and
completed runs remain in that batch, but incomplete repetitions are excluded from
the combined CSVs. A timeout refers to total process wall time, not just measured
search time. No failed or partial run is converted into a valid timing result.
The script does not resume a previous night automatically.

Configuration order is shuffled within each repetition, including capped/uncapped
modes. The random seeds and complete execution plans are saved. Only timing is
used to select deeper levels; observed move quality is not used for scheduling.
Results describe this benchmark and the tested workloads, not arbitrary positions.

2 MiB is the engine's minimum TT size. **512 MiB is the large experimental level**,
not the engine's theoretical maximum or kiloampere's memory limit. A giant table
can spend excessive time being cleared without adding useful search information.
The 64-MiB level connects the new tests with the earlier settings.

## Start on the server

After SSH-ing to kiloampere, check `hostname` and enter the checkout:

```bash
hostname
cd ~/weiss-tt-research
git status --short
```

Preserve any existing tracked server edits before switching branches, for example
with `git stash push -m "before overnight helper-depth experiment"`. Leave that
stash in place during the experiment; do not discard your edits or apply it during
a running build. Untracked compilation inputs under `src/` must also be recorded
before a real run; the runner checks this.

```bash
git fetch origin
git switch experiments/overnight-helper-depth
python3 scripts/run_overnight.py --dry-run
```

`git switch` creates a tracking branch automatically when only the remote branch
exists. This is an experimental branch; it has not been merged into master.
If you need to change settings, edit the clearly marked block at the top of
`scripts/run_overnight.py`. `--dry-run` launches no builds/searches and creates no
results. Optional `--pilot-only` runs just the depth-15 pilot for a short server
check; it does not start the full night or produce combined measurement CSVs.

Start the actual job in `tmux`:

```bash
tmux new -s weiss-night
python3 -u scripts/run_overnight.py --hours 6.5 2>&1 | tee overnight.log
```

Detach with **Ctrl-b, then d**. Reconnect using `tmux attach -t weiss-night`.
The job runs in the foreground inside tmux. SSH disconnection does not stop it.
If tmux is unavailable, use your server's established persistent-session method
before starting; an ordinary interactive SSH terminal does not provide this.

The script inherits and records CPU affinity and NUMA settings; it does not pin
workers or change placement. Keep one policy for the whole night. Exclusive use
was confirmed for this planned run. The affinity check still rejects a requested
worker count above the process's allowed CPUs. Do not run other benchmark jobs
alongside it or edit engine sources during the night.

## Output and the following morning

The script prints a new directory such as
`results/experiments/overnight-<UTC timestamp>/`. Preserve the **whole folder**,
including binaries, raw logs, plans, settings and patches. It is ignored by Git.

| File | Meaning |
|---|---|
| `runs.csv`, `positions.csv` in the night root | Combined non-pilot data, retaining only complete repetitions of each batch's entire configuration grid |
| `batches.csv` | Stage/depth, status, elapsed wall time, planned/completed/imported run counts |
| `settings.csv`, `overnight.py` | Settings, effective budget and a snapshot of the scheduling script |
| `status.txt` | Current stage or final status; inspect alongside `batches.csv` |
| `NN-baseline-depth15/`, `NN-depthD/`, `NN-extra-depthD/` | Original per-batch data, including anything excluded from combined CSVs |
| `NN-pilot-depthD/` | Calibration data, kept separate from measurements |

Combined run/repetition IDs are unique across batches; `batch_run` and
`batch_repetition` preserve the originals. `phase`, `batch` and adjusted `raw_log`
paths identify the source. If an incomplete repetition is excluded, its accepted
individual runs and any partial raw log still remain in the original batch.
An interrupt preserves completed data; inspect the status instead of assuming
that all planned runs finished. Budget/timeout termination is reported explicitly.

Existing fields retain their meanings. New fields are:

- `cap_helpers`: experiment mode; required for grouping comparable configurations.
- `process_wall_ns` in runs: full engine-process lifetime, including TT allocation,
  clearing, parsing and printing. The ordinary `wall_ns` still sums only timed
  search intervals, including search preparation and worker startup/joining.
- `helper_max_started_iteration`: deepest nominal iteration started by any helper.
- `helper_max_completed_iteration`: deepest nominal iteration completed by any helper.
- `helper_nodes_beyond_depth`: total helper move applications after completing the
  requested iteration. An unfinished requested iteration contributes zero to this
  metric. Capped runs and one-thread runs must report zero; the parser checks it.
  Quiescence/extended branches inside the requested iteration do not count as
  going beyond the iteration limit.

No TT probe counters or per-node atomic instrumentation were added. Iteration
bookkeeping is updated once per completed iteration; telemetry is printed after
joining helpers, outside the search timers.

For analysis on your pandas-equipped PC, set `EXPERIMENT_DIR` in the two summary
scripts to the **night root** and run:

```bash
python3 scripts/analyze_runs.py
python3 scripts/analyze_positions.py
```

For Stockfish comparison, also set `EXPERIMENT_DIR` and a new `OUTPUT_DIR` in
`scripts/compare_stockfish.py`. The scripts keep helper modes and executable hashes
separate; old CSVs without `cap_helpers` are treated as uncapped. The run summary
also reports full-process time and beyond-target helper work when available.

Compare capped/uncapped configurations at matched depth/hash/threads; compare TT
sizes at matched depth/mode/threads; compute speedup relative to the one-thread
configuration with the same depth/hash/mode. Inspect both per-position and suite
timings, moves, non-mate score variation and reference coverage. More throughput
alone is not evidence of faster time to depth or better moves. Higher depths have
fewer repetitions and a selected feasible range; do not hide missing configurations
or claim the timeout-selected range scales to arbitrary workloads.
