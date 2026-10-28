# Concurrent Transposition Tables in Parallel Chess Search

A bachelor’s thesis project investigating how transposition-table design affects
the performance and scalability of a parallel chess engine.

This repository is based on the
[Weiss chess engine](https://github.com/TerjeKir/weiss) by Terje Kirstihagen.
Weiss is a UCI-compatible chess engine distributed under the GNU General Public
License v3.0.

## Running experiments

The current runner uses the existing 50-position benchmark. Edit the settings at
the top of [`scripts/run_experiments.py`](scripts/run_experiments.py) to select
bucket sizes, depths, thread counts, TT memory sizes and repetitions. It builds
one binary per bucket size and executes the configured runs sequentially.

```bash
python3 scripts/run_experiments.py --dry-run  # preview only; no builds or searches
python3 scripts/run_experiments.py            # deliberately start the configured runs
```

Run it after SSH-ing into the experiment server and cloning this repository.
See [the experiment guide](docs/experiments.md) for server setup, editable settings,
SSH disconnects, CPU/NUMA placement, output files and measurement rules. Results
include per-position runtime, chosen move and score, plus complete raw logs and
build/machine metadata. Generated experiment directories are ignored by Git and
must be backed up separately.

## Research context

A transposition table stores information about positions that have already been
searched. This allows a chess engine to reuse results when the same position is
reached through a different sequence of moves.

In a parallel engine, multiple search threads may access the same table.
Sharing can reduce duplicated search, but it may also introduce contention,
cache-coherence traffic, inconsistent concurrent accesses, and destructive
replacement of useful entries.

The broader thesis will investigate the trade-off between information sharing
and concurrency overhead.

## Working research question

> How do different shared and thread-local transposition-table designs affect
> search efficiency and scalability in a parallel alpha-beta chess engine?

The final research question and experimental variants may be refined after the
proof of concept.

## Benchmark and TT configuration

Only `BUCKET_SIZE` is a build setting. It accepts 1, 2, 4 or 8 entries per bucket,
with default 2. At fixed TT memory, larger buckets mean fewer buckets and the same
total entry capacity. Depth, thread count and TT MiB are runtime arguments:

```bash
make -C src basic EXE=weiss-bench BUCKET_SIZE=2
./src/weiss-bench bench 12 8 64
#                         depth threads TT-MiB
```

Omitted arguments retain defaults of depth 14, 1 search worker and 32 MiB TT.
The worker count includes the main search worker. UCI option defaults are unchanged.
See the [experiment guide](docs/experiments.md) for output columns and timing scope.

## Historical proof of concept

The original direct-mapped/two-way comparison and Mac measurements remain in
[docs/poc.md](docs/poc.md). Those measurements preceded the current timing protocol
and must not be mixed with new timings. The experiment runner replaces the old
`scripts/run_poc.py`; the historical result files are preserved.

## Upstream baseline

The experimental work is based on:

- Upstream repository: [upstream_repository](https://github.com/TerjeKir/weiss)
- Upstream commit: `c735b8f3d2ddb0cdf42b135a5fb42e21c01f7a3d`
- Local baseline tag: `upstream-baseline-c735b8f`

The project intentionally uses a fixed upstream revision so that experiments
remain reproducible.

## Development platform

The initial proof of concept is developed and tested on:

- Machine: Apple MacBook Air with Apple M2
- Architecture: ARM64
- Operating system: macOS `26.6.2`
- Compiler: Apple Clang `17.0.0 (clang-1700.6.4.2)`

The planned experiment server is `kiloampere`: an Ampere-1a ARM64 machine with
192 physical cores, no SMT, four NUMA nodes and about 1 TiB RAM, as reported by
`lscpu`/`lsmem` in the supplied login transcript. Access is through the `hulk`
gateway. Server experiments have not been launched by this setup work.

## Building the upstream baseline

Install the Xcode Command Line Tools if they are not already available:

```bash
xcode-select --install
```
