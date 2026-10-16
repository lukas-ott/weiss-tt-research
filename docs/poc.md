# Proof of Concept

## Baseline

- Upstream commit: c735b8f3d2ddb0cdf42b135a5fb42e21c01f7a3d
- Machine: Apple MacBook Air with Apple M2
- Architecture: ARM64
- Operating system: macOS 26.6.2
- Compiler: Apple Clang 17.0.0 (clang-1700.6.4.2)
- Build commands:
  - `make -C src basic BUCKET_SIZE=1 EXE=weiss-direct`
  - `make -C src basic BUCKET_SIZE=2 EXE=weiss-two-way`
- Benchmark command: `./src/<executable> bench 12 <threads> 64`
- Repetitions: 5 per table/thread combination
- Hash: 64 MB
- Recorded run source commit: `0d04458a487e27edbc6bcc0c62623c7bccaec96b`

## Reproducing benchmark runs

Run `python3 scripts/run_poc.py` from the repository root. The default is depth
12, 64 MB, five repetitions, and 1, 2, 4, and 8 threads for both TT layouts.
For a short smoke test that also varies the hash budget, run:

```bash
python3 scripts/run_poc.py --threads 1 4 --hash-mb 16 64 --repetitions 2
```

The runner creates a new `results/poc/runs/<timestamp>/` directory containing
`runs.csv`, `run-info.txt`, build logs, and raw benchmark logs. Each CSV row
contains the relative path to its raw log. Raw `.log` files are ignored by Git,
so retain or share them separately if the exact run output needs to be audited.

## Thread-scaling observations

The table below is calculated from the recorded [40 individual runs](../results/poc/runs/20261016T133602040691Z/runs.csv).
The [run metadata](../results/poc/runs/20261016T133602040691Z/run-info.txt)
records the source state, commands, platform, and binary hashes. Values are
arithmetic means over five runs per row; the time range shows the fastest and
slowest run. NPS is the mean of the per-run NPS values printed by Weiss.

| Table | Threads | Mean time (ms) | Time range (ms) | Mean nodes | Mean NPS |
|---|---:|---:|---:|---:|---:|
| Direct-mapped | 1 | 1147.0 | 1137–1160 | 3,694,620 | 3,221,255 |
| Direct-mapped | 2 | 1088.4 | 1031–1170 | 6,785,923 | 6,239,751 |
| Direct-mapped | 4 | 1005.6 | 963–1049 | 12,421,642 | 12,352,207 |
| Direct-mapped | 8 | 1502.4 | 1235–1707 | 25,355,630 | 16,933,356 |
| Two-way | 1 | 1102.0 | 1086–1137 | 3,552,237 | 3,224,305 |
| Two-way | 2 | 1118.4 | 1054–1195 | 7,039,885 | 6,294,956 |
| Two-way | 4 | 1005.4 | 941–1062 | 12,376,131 | 12,312,309 |
| Two-way | 8 | 1449.2 | 1213–1657 | 24,106,785 | 16,670,811 |

Both layouts completed the benchmark. The timing ranges overlap, especially
at higher thread counts, so these results do not establish that either layout
is faster. Parallel runs can also search different node counts because threads
concurrently access and replace entries in the shared table. This is a proof of
concept for building and measuring both variants, not a controlled performance
evaluation.

## Status

- [x] ARM64 build
- [x] Baseline benchmark
- [x] Configurable search threads
- [x] Configurable hash size
- [x] Direct-mapped TT
- [x] Thread-scaling comparison
- [x] Structured logging
