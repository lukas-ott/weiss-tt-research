"""Check join cardinality, score perspective and missing/mate denominators."""

from pathlib import Path
import sys
import unittest

try:
    import pandas as pd
except ModuleNotFoundError:
    raise unittest.SkipTest("Stockfish comparison tests need the optional pandas dependency")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare_stockfish import CONFIG, compare, summarize


class StockfishComparisonTests(unittest.TestCase):
    def setUp(self):
        self.reference = pd.DataFrame([
            {"position": position, "rank": rank, "move": f"move{rank}",
             "score_cp": best - (rank - 1) * 20, "score_type": "cp",
             "fen": f"synthetic {'w' if position == 1 else 'b'}"}
            for position, best in [(1, 100), (2, -50)]
            for rank in range(1, 6)
        ])
        self.positions = pd.DataFrame([
            {"run": run, "repetition": run, "position": position,
             "bucket_size": 2, "depth": 15, "threads": 2, "hash_mb": 64,
             "binary_sha256": "build", "bestmove": move,
             "score": score, "score_type": score_type}
            for run, position, move, score, score_type in [
                (1, 1, "move2", 20, "cp"),
                (1, 2, "unknown", -30, "cp"),
                (2, 1, "move1", 110, "cp"),
                (2, 2, "move2", 123, "mate"),
            ]
        ])

    def test_unknown_moves_stay_missing_and_do_not_duplicate_rows(self):
        rows = compare(self.positions, self.reference)
        self.assertEqual(len(rows), 4)
        self.assertTrue(pd.isna(rows.loc[1, "move_gap_cp"]))
        self.assertTrue(pd.isna(rows.loc[1, "near_best"]))
        self.assertFalse(rows.loc[1, "move_in_top5"])
        self.assertEqual(rows.loc[0, "move_gap_cp"], 20)

    def test_black_scores_are_already_from_side_to_move(self):
        rows = compare(self.positions, self.reference)
        self.assertEqual(rows.loc[1, "score_difference_cp"], 20)  # -30 - (-50)

    def test_mates_are_excluded_from_cp_comparisons(self):
        rows = compare(self.positions, self.reference)
        self.assertTrue(pd.isna(rows.loc[3, "score_difference_cp"]))
        self.assertTrue(pd.isna(rows.loc[3, "chosen_score_difference_cp"]))
        self.reference.loc[self.reference["position"] == 2, "score_type"] = "mate"
        rows = compare(self.positions, self.reference)
        self.assertTrue(pd.isna(rows.loc[1, "score_difference_cp"]))
        self.assertTrue(pd.isna(rows.loc[3, "move_gap_cp"]))

    def test_summary_counts_and_denominators(self):
        summary = summarize(compare(self.positions, self.reference), CONFIG).iloc[0]
        self.assertEqual(summary["observations"], 4)
        self.assertEqual(summary["cp_comparisons"], 3)
        self.assertAlmostEqual(summary["mean_absolute_score_difference_cp"], 110 / 3)
        self.assertEqual(summary["best_move_agreement_pct"], 25)
        self.assertEqual(summary["top5_coverage_pct"], 75)
        self.assertEqual(summary["scored_moves"], 3)
        self.assertAlmostEqual(summary["mean_move_gap_cp"], 40 / 3)
        self.assertEqual(summary["near_best_pct_of_scored_moves"], 100)
        self.assertEqual(summary["chosen_cp_comparisons"], 2)
        self.assertEqual(summary["mean_absolute_chosen_score_difference_cp"], 35)

    def test_different_builds_are_kept_separate(self):
        self.positions.loc[self.positions["run"] == 2, "binary_sha256"] = "other-build"
        summary = summarize(compare(self.positions, self.reference), CONFIG)
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary["observations"].tolist(), [2, 2])
        self.assertEqual(summary["near_best_threshold_cp"].tolist(), [25, 25])

    def test_incomplete_suite_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "complete position suite"):
            compare(self.positions.iloc[:-1], self.reference)

    def test_duplicate_reference_move_is_rejected(self):
        duplicate = pd.concat([self.reference, self.reference.iloc[[0]]])
        with self.assertRaisesRegex(ValueError, "Duplicate reference"):
            compare(self.positions, duplicate)

    def test_invalid_reference_ranks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ranks 1..5"):
            compare(self.positions, self.reference.iloc[:-1])


if __name__ == "__main__":
    unittest.main()
