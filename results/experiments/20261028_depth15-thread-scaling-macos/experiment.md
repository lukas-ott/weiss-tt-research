# Depth 15 thread scaling on Apple M2

- **Run ID:** `20261028_depth15-thread-scaling-macos`
- **Status:** complete

## Question

How does increasing the total search worker count affect the runtime to requested depth 15 and the amount of search work on this Mac?

## Hypothesis

More workers may shorten wall time at first, but concurrent search overhead and search divergence may flatten or reverse the gain at higher counts.

## Settings

- Bucket size: 2 entries per bucket
- TT memory: 64 MiB
- Requested depth: 15
- Total workers: 1, 2, 4, 8 (main worker included)
- Repetitions: 5 per worker count
- Positions: the existing 50-position Weiss benchmark
- Helper cap: disabled
- Helper root move rotation: disabled
- Timeout: none
- Configuration order seed: 2026

## Controls

- The same 50 positions, source build, TT size and requested depth were used in every configuration.
- The runner randomized configuration order within each repetition.
- Searches ran sequentially with no per-worker CPU pinning; placement was inherited from the invoking process.
- Each run used a fresh benchmark process. The TT was cleared between positions according to the benchmark's existing protocol.

## Source and build identifiers

- Machine: Apple MacBook Air, Apple M2, macOS 26.6.2, arm64
- Compiler: Apple Clang 17.0.0
- Source commit: `0373a57bad9c8f36ed2c43a286375b447d534fe7`
- Source was modified before the run: `scripts/run_experiments.py` set depth 15 instead of the committed depth 12. The captured patch and runner snapshot are in the local run bundle.
- Bucket-2 executable SHA-256: `02c0cb5e4ff23c0790be21018fcbcc04836c153fff441365ce075ed20d0bf280`

## Results

Means and sample standard deviations are across five whole-suite runs. Node counts are summed across the 50 positions.

| Workers | Mean wall time (s) | SD (s) | Mean total nodes | Mean main-worker nodes | Mean CPU time (s) |
|---:|---:|---:|---:|---:|---:|
| 1 | 4.609 | 0.057 | 14,620,412 | 14,620,412 | 4.587 |
| 2 | 4.506 | 0.196 | 28,120,736 | 14,059,220 | 8.972 |
| 4 | 4.283 | 0.431 | 50,234,322 | 12,561,636 | 16.943 |
| 8 | 5.325 | 0.283 | 90,245,494 | 10,691,631 | 40.053 |

Four workers had the lowest mean wall time in this small Mac run, about 7% below one worker. Eight workers averaged about 16% slower than one worker. At eight workers, aggregate nodes were about 6.2 times the one-worker count while main-worker nodes were about 27% lower. Aggregate throughput therefore rose even though time to depth worsened.

## Conclusion

This run demonstrates why aggregate node throughput is not the same metric as time-to-depth: helpers add search work, while the main worker's node count and wall time follow different trends. It does not identify the cause of the slowdown at eight workers. The five repetitions, shallow fixed depth and Mac hardware are insufficient to generalize to kiloampere or establish a statistical effect. A main-worker target-depth timestamp and controlled server runs are needed to attribute the time spent.

## Limitations

- This is a MacBook Air measurement, not the 192-core ARM server.
- Only 1–8 workers, one TT size, one bucket size and one depth were measured.
- Results are descriptive; the sample standard deviations overlap and no inferential test was performed.
- CPU placement and frequency were not pinned or independently controlled.
- This benchmark measures search completion time and work, not playing strength.
