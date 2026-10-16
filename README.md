# Concurrent Transposition Tables in Parallel Chess Search

A bachelor’s thesis project investigating how transposition-table design affects
the performance and scalability of a parallel chess engine.

This repository is based on the
[Weiss chess engine](https://github.com/TerjeKir/weiss) by Terje Kirstihagen.
Weiss is a UCI-compatible chess engine distributed under the GNU General Public
License v3.0.

## Project status

The project is currently in the proof-of-concept phase.

The objective of the proof of concept is to demonstrate that:

1. the transposition-table implementation can be replaced without redesigning
   the engine’s search;
2. search-thread count and transposition-table memory can be controlled;
3. engine behavior and performance can be logged reproducibly;
4. the experimental setup runs on ARM64.

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

## Proof-of-concept scope

The initial proof of concept compares two simple table organizations:

- **Two-way bucketed table:** the original Weiss implementation with two entries
  per bucket.
- **Direct-mapped table:** an alternative implementation with one entry per
  bucket.

Build both table organizations with:

```bash
make -C src basic BUCKET_SIZE=1 EXE=weiss-direct
make -C src basic BUCKET_SIZE=2 EXE=weiss-two-way
```

`BUCKET_SIZE=1` selects the direct-mapped table. `BUCKET_SIZE=2` selects the
original two-way bucketed table and is the default when no value is provided.

Run a recorded comparison with:

```bash
python3 scripts/run_poc.py --threads 1 2 4 8 --hash-mb 64 --repetitions 5
```

The runner builds both binaries, alternates their run order between repetitions,
and creates a new directory under `results/poc/runs/`. Each run has a raw engine
log and a row in `runs.csv`; `run-info.txt` records the machine, Git state,
commands, and binary hashes. Use `--help` to see smaller test configurations or
`--skip-build` to run binaries that were already built.

### Thread-scaling experiment

Both variants were benchmarked five times at depth 12 with a 64 MB
transposition table and 1, 2, 4, and 8 search threads. See the
[proof-of-concept results](docs/poc.md#thread-scaling-observations) for the
summary, [individual runs](results/poc/runs/20261016T133602040691Z/runs.csv),
and limitations. The observed timing variation does not support a claim that
either layout is faster.

### Progress

- [x] Record the exact upstream baseline
- [x] Verify the unmodified ARM64 build
- [x] Record a baseline benchmark
- [x] Make the TT organization selectable at compile time
- [x] Build the original two-way TT
- [x] Build the direct-mapped TT
- [x] Verify configurable pthread counts
- [x] Verify configurable TT memory sizes
- [x] Add structured experiment logging
- [x] Document the proof-of-concept results

## Upstream baseline

The experimental work is based on:

- Upstream repository: https://github.com/TerjeKir/weiss
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

Large-scale experiments will later be run on an ARM-based university server.
That server is outside the scope of the initial proof of concept.

## Building the upstream baseline

Install the Xcode Command Line Tools if they are not already available:

```bash
xcode-select --install
```
