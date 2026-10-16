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

## Reproducing benchmark runs

Run `python3 scripts/run_poc.py` from the repository root. The default is depth
12, 64 MB, five repetitions, and 1, 2, 4, and 8 threads for both TT layouts.
For a short smoke test that also varies the hash budget, run:

```bash
python3 scripts/run_poc.py --threads 1 4 --hash-mb 16 64 --repetitions 2
```

The runner creates a new `results/poc/runs/<timestamp>/` directory containing
`runs.csv`, `run-info.txt`, build logs, and raw benchmark logs. Each CSV row
links to its raw log. Raw `.log` files are ignored by Git, so retain or share
them separately if the exact run output needs to be audited.

## Thread-scaling observations

The table reports arithmetic means over five runs. Time is in milliseconds and
NPS is the mean of the per-run NPS values printed by the benchmark.

| Table | Threads | Time (ms) | Nodes | NPS |
|---|---:|---:|---:|---:|
| Direct-mapped | 1 | 1203.6 | 3,694,620 | 3,069,794 |
| Direct-mapped | 2 | 1149.8 | 7,028,384 | 6,113,117 |
| Direct-mapped | 4 | 1116.0 | 12,561,720 | 11,257,196 |
| Direct-mapped | 8 | 3367.8 | 23,239,055 | 8,469,607 |
| Two-way | 1 | 2477.8 | 3,552,237 | 1,433,952 |
| Two-way | 2 | 1558.2 | 7,005,713 | 4,897,776 |
| Two-way | 4 | 1144.6 | 12,491,176 | 10,933,995 |
| Two-way | 8 | 1516.4 | 22,791,003 | 15,041,980 |

The direct-mapped table was faster in the single-thread sample, while the
two-way table was faster at eight threads. The direct-mapped eight-thread
measurements showed substantial timing variation. Parallel runs can also
produce different node counts because threads concurrently access and replace
entries in the shared transposition table. These results should therefore be
treated as an initial proof of concept rather than a controlled performance
evaluation.

## Status

- [x] ARM64 build
- [x] Baseline benchmark
- [x] Configurable search threads
- [x] Configurable hash size
- [x] Direct-mapped TT
- [x] Thread-scaling comparison
- [x] Structured logging
