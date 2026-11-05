"""Synthetic scheduling/data checks; no Weiss executable or server is used."""

import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_overnight as night


class OvernightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "night"
        self.output.mkdir()
        names = ["BUCKET_SIZES", "DEPTHS", "THREAD_COUNTS", "HASH_SIZES_MB",
                 "CAP_HELPERS", "REPETITIONS", "ORDER_SEED", "TIMEOUT_SECONDS", "CC"]
        saved = {name: getattr(night.runner, name) for name in names}
        self.addCleanup(lambda: [setattr(night.runner, key, value) for key, value in saved.items()])

    def batch_data(self, name):
        batch = self.output / name
        batch.mkdir()
        # Repetition 1 is complete; repetition 2 only has run 3.
        night.append_csv(batch / "runs.csv", [
            {"run": i, "repetition": 1 if i <= 2 else 2,
             "raw_log": f"raw/run-{i:04d}.log"} for i in [1, 2, 3]
        ])
        night.append_csv(batch / "positions.csv", [
            {"run": i, "repetition": 1 if i <= 2 else 2, "position": p,
             "raw_log": f"raw/run-{i:04d}.log"} for i in [1, 2, 3, 4] for p in [1, 2]
        ])
        plan = [{"run": i, "repetition": 1 if i <= 2 else 2} for i in [1, 2, 3, 4]]
        return batch, plan

    def test_only_balanced_repetitions_are_combined_and_ids_are_unique(self):
        a, plan = self.batch_data("a")
        b, _ = self.batch_data("b")
        self.assertEqual(night.collect_balanced(a, self.output, "baseline", plan), 2)
        self.assertEqual(night.collect_balanced(b, self.output, "depth", plan), 2)
        runs = night.read_csv(self.output / "runs.csv")
        self.assertEqual([r["run"] for r in runs], ["1", "2", "3", "4"])
        self.assertEqual([r["repetition"] for r in runs], ["1", "1", "2", "2"])
        self.assertEqual(runs[2]["batch_run"], "1")
        self.assertEqual(runs[2]["raw_log"], "b/raw/run-0001.log")
        self.assertEqual(len(night.read_csv(self.output / "positions.csv")), 8)
        self.assertEqual(len(night.read_csv(a / "runs.csv")), 3)  # Inputs unchanged.

    def test_pilot_data_is_not_combined(self):
        with patch.object(night.runner, "execute"):
            ok, _ = night.run_batch(self.output, "pilot", 15, [1], [2], 1, 100)
        self.assertTrue(ok)
        self.assertFalse((self.output / "runs.csv").exists())
        journal = night.read_csv(self.output / "batches.csv")
        self.assertEqual(journal[0]["phase"], "pilot")
        self.assertEqual(journal[0]["balanced_runs_imported"], "0")

    def test_timeouts_are_recorded_without_becoming_successful_runs(self):
        with patch.object(night.runner, "execute", side_effect=subprocess.TimeoutExpired("fake", 1)):
            ok, _ = night.run_batch(self.output, "pilot", 15, [1], [2], 1, float("inf"))
        self.assertFalse(ok)
        self.assertEqual(night.read_csv(self.output / "batches.csv")[0]["status"], "timeout")
        self.assertFalse((self.output / "runs.csv").exists())

    def test_budget_estimate_reserves_time_and_accounts_for_repetitions(self):
        with patch.object(night.time, "monotonic", return_value=1000):
            self.assertTrue(night.fits(10, 5, 1700))
            self.assertFalse(night.fits(10, 5, 1600))

    def test_dry_run_creates_no_output_or_subprocesses(self):
        with patch.object(sys, "argv", ["run_overnight.py", "--dry-run"]):
            with patch.object(night.runner, "ROOT", self.root), patch.object(night, "run_batch") as batch:
                night.main()
        batch.assert_not_called()
        self.assertFalse((self.root / "results").exists())

    def test_scheduler_runs_baseline_then_pilot_then_measurements_and_stops(self):
        clock = [0.0]
        phases, deadlines = [], []

        def fake_batch(output, phase, depth, threads, hashes, repetitions, deadline):
            phases.append(phase)
            deadlines.append(deadline)
            cost = {"baseline": 5, "pilot": 2, "depth": 4, "extra": 2}[phase]
            clock[0] += cost
            return True, cost

        with patch.multiple(night, BASELINE_THREADS=[1], DEPTH_THREADS=[1],
                            DEPTHS=[15], HASH_SIZES_MB=[2], BASELINE_REPETITIONS=1,
                            DEPTH_REPETITIONS=2, RESERVE_SECONDS=1):
            with patch.object(sys, "argv", ["run_overnight.py", "--hours", "0.01"]):
                with patch.object(night.runner, "ROOT", self.root), patch.object(night.runner, "capture", return_value=""):
                    with patch.object(night.time, "monotonic", side_effect=lambda: clock[0]):
                        with patch.object(night, "run_batch", side_effect=fake_batch):
                            night.main()
        self.assertEqual(phases[:3], ["baseline", "pilot", "depth"])
        self.assertTrue(all(phase == "extra" for phase in phases[3:]))
        self.assertLess(deadlines[1], deadlines[0])  # Pilot cannot consume the entire night.
        self.assertLess(clock[0], 36)
        result = next((self.root / "results/experiments").iterdir())
        self.assertTrue((result / "status.txt").read_text().startswith("finished"))


if __name__ == "__main__":
    unittest.main()
