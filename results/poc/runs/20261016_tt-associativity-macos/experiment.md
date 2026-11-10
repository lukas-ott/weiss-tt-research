# Direct-mapped versus two-way TT proof of concept

- **Run ID:** `20261016_tt-associativity-macos`
- **Status:** complete

## Question

Does a two-entry bucket change benchmark runtime or search work compared with a direct-mapped table at the same nominal TT memory size?

## Hypothesis

Two-way buckets may preserve useful entries longer and improve reuse, but selection and replacement overhead could offset that benefit. This proof of concept was exploratory, not a controlled final comparison.

## Settings

- Table layouts: 1-entry direct mapped and 2-entry bucket
- TT memory: 64 MiB
- Requested depth: 12
- Total workers: 1, 2, 4, 8 (main worker included)
- Repetitions: 5 per table/thread configuration
- Benchmark: the then-current 50-position Weiss suite
- Runs: 40 completed benchmark processes

## Controls

- Both variants used the same benchmark and nominal TT memory.
- Thread counts and repetitions were matched between layouts.
- The runner randomized the configuration order.
- Measurements were made on the same Apple M2 laptop.

## Source and build identifiers

- Machine: Apple MacBook Air, Apple M2, macOS 26.6.2, arm64
- Compiler: Apple Clang 17.0.0
- Source commit: `0d04458a487e27edbc6bcc0c62623c7bccaec96b`
- Direct-map executable SHA-256: `5f0497f196a1a089f782fb7e65f8c945b028e558ea63c28d90fa122a60a26956`
- Two-way executable SHA-256: `169871a0984d85f8df77bcd1c633feb141687648f1af4ea4e4ca2f2dfeedb611`
- Commands and run metadata are in `run-info.txt`; available logs are in `raw/` where retained.

## Results

Mean elapsed milliseconds over five runs per configuration:

| Layout | Workers | Mean time (ms) | Mean nodes |
|---|---:|---:|---:|
| Direct mapped | 1 | 1147.0 | 3,694,620 |
| Direct mapped | 2 | 1088.4 | 6,785,923 |
| Direct mapped | 4 | 1005.6 | 12,421,642 |
| Direct mapped | 8 | 1502.4 | 25,355,630 |
| Two-way | 1 | 1102.0 | 3,552,237 |
| Two-way | 2 | 1118.4 | 7,039,885 |
| Two-way | 4 | 1005.4 | 12,376,131 |
| Two-way | 8 | 1449.2 | 24,106,785 |

## Conclusion

The measurements do not establish that either table layout is faster. The table-specific means are close at one and four workers, and the timing ranges overlap. At eight workers, both layouts take longer than at four while searching more nodes. This supports using a more controlled benchmark protocol; it does not prove why the high-worker slowdown occurs.

## Limitations

- Exploratory Mac results at depth 12; not representative of the 192-core server.
- The prior benchmark timing included thinking-output overhead and predates the current timing protocol.
- Only five repetitions per configuration; no significance test was performed.
- Parallel searches may visit different nodes due to shared-table updates and scheduling.
- This tests benchmark metrics, not playing strength.
