# Proof of Concept

## Baseline

- Upstream commit: 735b8f3d2ddb0cdf42b135a5fb42e21c01f7a3d
- Machine: m2 Macbook air
- Operating system: MacOS 26.6.2
- Compiler: Apple Clang 17.0.0 (clang-1700.6.4.2)
- Build command: make clean make basic
- Benchmark depth: 12

## Baseline observations

| Backend | Threads | Hash | Time | Nodes | NPS |
|---|---:|---:|---:|---:|---:|
| Original | 1 | 64 MB | 1299 | 3552237 | 2.734.593 |
| Original | 4 | 64 MB | 1089 | 12467969 | 11.449.007 |
| Original | 8 | 64 MB | 1187 | 19995063 | 16.845.040 |

## Status

- [x] ARM64 build
- [x] Baseline benchmark
- [x] Configurable search threads
- [x] Configurable hash size
- [ ] Direct-mapped TT
- [ ] Structured logging