# Stockfish 19 reference for the Weiss benchmark

The final reference is `top5.csv`: one row for each of the five ranked candidates
for each of the 50 benchmark positions, in the original position order.

Settings: Stockfish 19, nominal depth 28, MultiPV 5, one search thread, 512 MiB
hash per engine. Each position uses a fresh process and `ucinewgame`. Two
independent positions may run simultaneously; they share no engine state.
Full strength is enabled. No Syzygy tablebases are configured. The exact FEN,
including its halfmove clock, is used; earlier game history is unavailable.

## Reading the CSV

- `position`: original Weiss benchmark position ID, 1 to 50.
- `rank`: Stockfish's candidate order at the completed target depth, 1 to 5.
- `move`: UCI coordinate notation, e.g. `e1g1` for White's kingside castling.
  A promotion has an extra piece letter, e.g. `a7a8q`.
- `side_to_move`: `w` or `b`. **All evaluations are from this side's perspective.**
- `score_type`: `cp` or `mate`; `score` is the original signed UCI value.
- `score_cp` and `score_pawns`: numeric evaluation and its value divided by 100;
  blank for mate scores. Modern Stockfish scores are normalized, and should not
  be treated as a direct material count or compared numerically to Weiss as if
  both engines used an identical evaluation scale.
- `mate_moves`: signed distance in moves; positive means the side to move can
  deliver mate, negative means it is being mated. Blank for centipawn scores.
- `depth` / `seldepth`: nominal iteration depth and the reported selective depth.
- `nodes`, `nps`, `time_ms`, `hashfull_permille`, `tbhits`: UCI search statistics.
  These are cumulative search statistics for the position, **not separate costs
  of the individual candidate**; do not sum them over ranks.
- `wdl_win`, `wdl_draw`, `wdl_loss`: Stockfish's approximate outcome model,
  expressed per 1,000 and from the side-to-move perspective. These are model
  estimates, not measured frequencies for these particular positions.
- `pv`: complete principal variation reported for that candidate.

`iterations.csv` retains the last score/PV record without a bound flag for each
rank at each reported depth,
`positions.csv` lists the original FENs, and `settings.csv` records provenance and
settings, including engine hash and analysis timestamps. Additional metadata,
raw UCI logs and per-position JSON snapshots are preserved in the local download
archive, not committed here. The CSV `raw_log` fields refer to paths inside that
archive.

A completed nominal depth is not a full-width minimax guarantee. Small evaluation
or rank differences between close moves should not be overinterpreted. MultiPV
itself affects the search: superseded MultiPV-3 pilot runs are excluded from the
final reference. This is an independent analysis reference, not mathematical
ground truth or an engine-strength test.

Official sources:
- https://github.com/official-stockfish/Stockfish/releases/tag/sf_19
- https://official-stockfish.github.io/docs/stockfish-wiki/UCI-Protocol-and-Stockfish-Commands.html
- https://official-stockfish.github.io/docs/stockfish-wiki/Stockfish-FAQ.html
