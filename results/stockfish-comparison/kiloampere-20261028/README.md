# Kiloampere configurations compared with Stockfish 19

The configurations with more threads do **not** show worse agreement with this
reference overall. From 1 to 128 threads, exact best-move agreement rises from
54.0% to 62.4%, and the mean absolute numeric score difference falls from
106.34 to 94.72 cp. Neither trend is monotonic. These observations do not establish
playing strength, statistical significance, or the cause of limited time-to-depth scaling.

Stockfish's depth-28 analysis is an independent **reference**, not proven ground
truth. Weiss and Stockfish use different evaluation scales and search procedures.
The raw difference between their scores is therefore not a calibrated chess error.

## Data and reproduction

- Weiss batch: `20261028T144854664254Z`, run on `kiloampere`, Ampere-1a ARM64,
  192 physical cores, four NUMA nodes.
- Fixed parameters: nominal depth 15, 64 MiB TT, two entries per bucket.
  Threads: 1, 2, 4, 8, 16, 32, 64 and 128.
- Five repetitions of the ordered 50-position benchmark per configuration:
  **40 runs and 2,000 position searches**. Only thread count varies here.
- Weiss source: `0373a57bad9c8f36ed2c43a286375b447d534fe7`, plus local runner settings for
  depth 15 and the thread list above. The engine sources were unchanged.
  GCC 13.3.0, existing non-PGO `basic` build.
- Executable SHA-256:
  `4050180a41099c77db867e018d8215aec017b6c38ddbaf1a8d678639c2da7122`.
- Reference: [Stockfish 19 CSVs](../../stockfish19-reference-20261029/README.md),
  depth 28, MultiPV 5, one thread, 512 MiB TT, fresh process per position,
  no tablebases. Both engines' CSV scores are from the **side to move**.
  All scores in this particular comparison are centipawn scores.

The original position/run CSVs, execution plan, build command, machine metadata,
settings patch, raw logs and binaries remain in the local server-experiment archive.
They are not included in this Git dataset. Published files contain the Stockfish
reference and derived analysis summaries. No new engine searches were run for this
comparison. Before publication, all 40 raw Weiss logs were checked against the CSV
records, requested parameters, clock/node totals and ordered FENs. The copied
binary hash was checked too.

To regenerate, make the original batch available locally and select its folder
with `EXPERIMENT_DIR` near the top of `scripts/compare_stockfish.py`. Its default is
`results/experiments/20261028T144854664254Z`, containing `positions.csv`.
Install the existing analysis dependency in your environment, then run from the
repository root:

```bash
python -m pip install -r requirements-analysis.txt
python scripts/compare_stockfish.py
```

The script's marked settings block selects the input, reference, output directory
and near-best threshold. It runs no chess searches. It rejects duplicate reference
moves, incomplete suites and inconsistent configurations within a run, checks the
reference FENs against `src/tests.c`, and keeps configurations/build hashes separate.
For a new experiment, its position IDs must still refer to this same FEN suite;
the runner's position CSV itself contains no FEN column.

## Numeric evaluation differences

For each observation, `score_difference_cp = Weiss score - Stockfish rank-1 score`.
Mean absolute difference (MAE) averages its absolute value. Positive signed bias
means Weiss reports a more favorable number for the side to move. Mate distances
are excluded from all centipawn arithmetic.

| Threads | Mean absolute difference, cp | Median absolute difference, cp | Signed bias, cp | SD of repetition MAE, cp |
|---:|---:|---:|---:|---:|
| 1 | 106.34 | 57.0 | +7.98 | 0.00 |
| 2 | 105.26 | 59.5 | +0.74 | 3.41 |
| 4 | 101.98 | 51.5 | +1.04 | 2.86 |
| 8 | 101.70 | 53.0 | +1.33 | 3.10 |
| 16 | 101.97 | 53.5 | +2.47 | 3.00 |
| 32 | 97.85 | 53.5 | -0.06 | 2.60 |
| 64 | 98.17 | 54.0 | -0.08 | 4.20 |
| 128 | 94.72 | 55.5 | +4.07 | 7.20 |

Each row contains 250 observations: 50 positions times five repetitions. The SD is
the sample standard deviation of five **whole-suite MAEs**, not a standard error
computed by pretending that 250 repeated position searches are independent.
The one-thread searches reproduce the same scores/moves in every repetition;
their timing still varies. Small signed biases conceal substantial differences
because positive and negative differences cancel.

A few positions dominate the numeric discrepancy. Averaging equally over all
configurations/repetitions, the five largest are:

| Position | SF best move | SF best score, cp | Mean Weiss score, cp | Mean absolute difference, cp |
|---:|:---|---:|---:|---:|
| 5 | e3e4 | +1004 | +383.43 | 620.58 |
| 18 | b1b6 | -676 | -215.40 | 460.60 |
| 38 | d6c6 | +804 | +1181.40 | 377.40 |
| 35 | d1c1 | +592 | +233.58 | 358.43 |
| 10 | c5b4 | +594 | +299.50 | 294.50 |

These five of 50 positions contribute **41.8%** of total absolute score difference.
In position 5, every one of the 40 searches chooses Stockfish's best move `e3e4`,
despite the large numeric discrepancy. A large score difference is not by itself
evidence of a bad move. Scores include effects of scale, evaluation function,
search depth, selectivity and move choice; this analysis does not isolate them.

`chosen_score_difference_cp` additionally compares Weiss's score with Stockfish's
score for the **same chosen move**, where available. This removes the difference
in move choice from the comparison target, but does not align the two engines'
evaluation scales or search methods. Its MAE is 119.39 cp at one thread and
101.78 cp at 128 threads, on different covered subsets.

## Move choice using only Stockfish's evaluation scale

Exact agreement means selecting Stockfish's recorded rank-1 move. It does not mean
all other moves are mistakes: close alternatives, including equally evaluated
draws, can have different ranks.

For a chosen move present in the saved top five, define:

`move_gap_cp = SF rank-1 score - SF score of the Weiss move`

Both scores now come from the same Stockfish MultiPV search. This is a finite-depth
estimate of the evaluation given up by that choice. A move outside the saved five
has **unknown** gap; it is not assigned zero, rank six, or a made-up penalty.
"Within 25 cp" is a descriptive tolerance, not a validated mistake threshold.

| Threads | Exact best agreement | Top-five coverage | Scored choices / 250 | Mean gap, scored only, SF cp | Within 25 cp, scored only |
|---:|---:|---:|---:|---:|---:|
| 1 | 54.0% | 88.0% | 220 | 13.34 | 86.36% |
| 2 | 50.0% | 88.8% | 222 | 10.72 | 88.74% |
| 4 | 57.6% | 92.0% | 230 | 10.31 | 89.57% |
| 8 | 55.6% | 92.4% | 231 | 9.66 | 89.61% |
| 16 | 56.8% | 92.0% | 230 | 8.09 | 91.74% |
| 32 | 59.2% | 94.0% | 235 | 9.84 | 91.06% |
| 64 | 60.0% | 90.8% | 227 | 6.90 | 95.15% |
| 128 | 62.4% | 94.8% | 237 | 7.74 | 92.41% |

Across all configurations, **168/2,000 observations (8.4%)** lack a chosen-move
reference score: 26 distinct position/move pairs across 14 positions. Their
frequencies and configurations are listed in [unscored_moves.csv](unscored_moves.csv).
These conditional mean gaps must not be treated as complete loss estimates or
used alone to rank configurations: their covered subsets differ.

As a sensitivity check, [common_positions.csv](common_positions.csv) uses the
same **36 positions** for which every returned move in every configuration and
repetition has a numeric reference score: 180 choices per configuration.

| Threads | Mean gap on common 36 positions, SF cp |
|---:|---:|
| 1 | 15.56 |
| 2 | 11.69 |
| 4 | 10.10 |
| 8 | 10.59 |
| 16 | 8.62 |
| 32 | 10.10 |
| 64 | 7.31 |
| 128 | 7.82 |

This check also shows no overall deterioration with more threads. It remains a
selected subset and excludes positions with unscored alternatives, potentially
including important failures. It cannot establish quality over all 50 positions.

For a concrete example of score instability without move instability, position 21
at 128 threads returns `c2c4` in all five runs, with Weiss scores
`-724, -1094, -970, -988, -999` cp: a 370-cp range for the same move. Stockfish scores
that move at -1203 cp and its preferred `f2e1` at -1072 cp, a 131-SF-cp gap.
Both alternatives are already evaluated as heavily losing. The score spread does
not represent five different move choices, nor can it be read as 370 cp of lost
playing strength.

The reference itself has uncertainty: comparing rank-1 records at depths 27 and
28 in `iterations.csv`, the best move changes in **8/50 positions**. The mean
absolute best-score change is 7.18 SF cp, maximum 55 cp. This is a local stability
check, not an error bound. It further limits interpretations of exact agreement
or tiny candidate-score gaps.

## Runtime and experimental implications

| Threads | Mean suite wall time, s | Sample SD, s | Speedup relative to one thread |
|---:|---:|---:|---:|
| 1 | 7.057 | 0.035 | 1.00x |
| 2 | 7.033 | 0.265 | 1.00x |
| 4 | 6.110 | 0.145 | 1.16x |
| 8 | 5.013 | 0.233 | 1.41x |
| 16 | 4.242 | 0.329 | 1.66x |
| 32 | 3.519 | 0.126 | 2.01x |
| 64 | 3.264 | 0.153 | 2.16x |
| 128 | 2.914 | 0.163 | 2.42x |

These are sums of measured per-position search intervals. TT allocation/clearing,
FEN parsing and output are excluded; search preparation and worker startup/joining
are included. TT state starts empty per position, while worker histories/caches
persist through the ordered suite. No individual worker pinning was used, and
exclusive server access was not established by these records.

From 32 to 128 threads, four times the workers reduce mean elapsed time by about
17.2%. The overall quality metrics above do not indicate a compensating collapse
in move quality. This is still a short, fixed-depth workload on a particular
placement policy. It cannot identify whether search duplication, startup costs,
TT effects, NUMA, frequency or other factors cause the limited speedup.

For the next experiment:

1. Repeat at deeper workloads and record both elapsed time to depth and these
   quality checks. Use whole-suite repetitions; keep machine/placement/builds
   separate. A fixed-depth speedup alone need not mean an equivalent amount of
   selective search or equivalent strength.
2. For a complete move-quality comparison, analyze **all observed candidate moves**
   alongside the reference candidates in the same per-position Stockfish search,
   including the 26 missing pairs. Check stability at higher depth for large gaps
   and close rankings. Do not infer missing-move scores from their absence.
3. Compare TT variants at matched depth, memory, thread counts and placement.
   Only thread count varies in this dataset; it cannot tell us whether a different
   bucket size, locking scheme or private/shared TT design is better.
4. If a playing-strength claim is needed, use controlled games or a larger,
   independently chosen test set. This benchmark/reference comparison is a
   diagnostic, not an Elo measurement.

## Output files

| File | Contents |
|---|---|
| `observations.csv` (generated locally; not uploaded) | All 2,000 searches, FEN, reference best move/score, chosen-move rank/score, differences, coverage |
| [configurations.csv](configurations.csv) | Eight configuration summaries, repetition counts, runtime means/SDs and quality metrics |
| [repetitions.csv](repetitions.csv) | Forty whole-suite summaries; use these to inspect run-to-run variation |
| [positions.csv](positions.csv) | 400 configuration/position summaries, distinct-move counts and Weiss score SDs |
| [common_positions.csv](common_positions.csv) | Quality metrics restricted to the same 36 fully scored positions |
| [unscored_moves.csv](unscored_moves.csv) | Missing reference moves and occurrence counts per configuration |

All CP fields are centipawns. Cross-engine differences are numeric discrepancies;
`move_gap_cp` uses Stockfish's scale alone. `*_pct` fields are percentages, blanks
mean unavailable, and standard deviations use `n-1`. `near_best_threshold_cp` records the tolerance;
for these outputs it is 25 SF cp. Input CSVs remain unchanged when rerunning the script.
