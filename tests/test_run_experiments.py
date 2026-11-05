"""Runner checks using synthetic logs and fake commands; never run Weiss."""

import csv
import importlib.util
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_experiments.py"
spec = importlib.util.spec_from_file_location("runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def log_text(depth=4, threads=1, memory=4, bucket=2):
    lines = [f"BENCH_CONFIG depth={depth} threads={threads} hash_mb={memory} bucket_size={bucket} "
             f"entry_bytes=16 bucket_bytes={16*bucket} buckets={memory*1024*1024//(16*bucket)} cap_helpers=0",
             "BENCH_POSITION_HEADER," + ",".join(runner.POSITION_FIELDS)]
    for i in range(1, 51):
        score = "mate,1,30999" if i == 1 else "cp,-20,-20"
        lines.append(f"BENCH_POSITION,{i},{depth},1000000,1000000,10,{10*threads},e2e4,{score},0,0,0")
    lines += ["BENCH_TOTAL wall_ns=50000000 cpu_ns=50000000",
              f"OVERALL: 50 ms {500*threads} nodes {10000*threads} nps"]
    return "\n".join(lines) + "\n"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = dict(bucket_size=2, depth=4, threads=1, hash_mb=4, cap_helpers=0)
        self.log = self.root / "raw.log"

    def parse(self, text, expected=None):
        self.log.write_text(text)
        return runner.parse_result(self.log, expected or self.expected)

    def test_preserves_moves_scores_and_exact_totals(self):
        summary, rows = self.parse(log_text())
        self.assertEqual((summary["wall_ns"], summary["nodes"]), (50_000_000, 500))
        self.assertEqual((rows[0]["score_type"], rows[0]["score"], rows[0]["raw_score"]),
                         ("mate", 1, 30999))
        self.assertEqual(rows[1]["score"], -20)

    def test_rejects_incomplete_corrupt_or_wrong_binary_output(self):
        original = log_text()
        cases = [original.replace("BENCH_POSITION,50,", "DISCARDED,50,"),
                 original.replace("BENCH_POSITION,50,", "BENCH_POSITION,49,"),
                 original.replace("wall_ns=50000000", "wall_ns=1"),
                 original.replace("OVERALL: 50 ms", "OVERALL: 51 ms"),
                 original.replace("threads=1", "threads=2"),
                 original.replace("bucket_bytes=32", "bucket_bytes=64"),
                 original.replace("entry_bytes=16 ", ""),
                 original.replace("BENCH_POSITION,5,4,", "BENCH_POSITION,5,3,"),
                 original.replace("e2e4", "not-a-move"),
                 original + "BENCH_TOTAL wall_ns=50000000 cpu_ns=50000000\n"]
        for text in cases:
            with self.subTest(text=text[-120:]), self.assertRaises(ValueError):
                self.parse(text)

    def test_cap_modes_are_separate_and_checked_against_engine_output(self):
        plan = runner.make_plan(caps=[0, 1])
        self.assertEqual({r["cap_helpers"] for r in plan}, {0, 1})
        expected = dict(self.expected, cap_helpers=1)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.parse(log_text(), expected)
        text = log_text(threads=2).replace("cap_helpers=0", "cap_helpers=1")
        self.parse(text.replace("cp,-20,-20,0,0,0", "cp,-20,-20,4,4,0"),
                   dict(expected, threads=2))
        for telemetry in ["5,4,0", "5,4,1"]:
            with self.subTest(telemetry=telemetry), self.assertRaisesRegex(ValueError, "helper telemetry"):
                self.parse(text.replace("cp,-20,-20,0,0,0", "cp,-20,-20," + telemetry),
                           dict(expected, threads=2))

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_timeout_stops_the_whole_child_process_group(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("fake", 1), 0]
        with patch.object(runner.subprocess, "Popen", return_value=process):
            with patch.object(runner.os, "killpg") as kill:
                with self.assertRaises(subprocess.TimeoutExpired):
                    runner.run_command(["fake"], None, 1)
        kill.assert_called_once_with(12345, runner.signal.SIGKILL)

    def test_budget_timeout_uses_remaining_wall_time(self):
        with patch.object(runner.time, "monotonic", return_value=100):
            self.assertEqual(runner.budget_timeout(110, 300), 10)
            self.assertEqual(runner.budget_timeout(110, 2), 2)
            self.assertIsNone(runner.budget_timeout(None))
            with self.assertRaises(runner.BudgetExpired):
                runner.budget_timeout(99)

    def test_plan_has_every_combination_once_per_repetition_and_is_repeatable(self):
        with patch.multiple(runner, BUCKET_SIZES=[1, 2], DEPTHS=[4, 5], THREAD_COUNTS=[1, 2],
                            HASH_SIZES_MB=[4], REPETITIONS=2):
            a, b = runner.make_plan(), runner.make_plan()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        for repetition in [1, 2]:
            combinations = {(r["bucket_size"], r["depth"], r["threads"], r["hash_mb"])
                            for r in a if r["repetition"] == repetition}
            self.assertEqual(len(combinations), 8)

    def test_normal_plan_contains_only_uncapped_baseline_and_sweep(self):
        plan = runner.make_experiment_plan()
        self.assertEqual([r["run"] for r in plan], list(range(1, len(plan) + 1)))
        self.assertEqual({r["phase"] for r in plan}, {"baseline", "depth-sweep"})
        self.assertTrue(all(r["cap_helpers"] == 0 for r in plan))
        sweep = [r["depth"] for r in plan if r["phase"] == "depth-sweep"]
        self.assertEqual(sweep, sorted(sweep))

    def test_time_budget_applies_without_a_special_flag(self):
        plan = runner.make_plan(depths=[4], threads=[1], hashes=[4], repetitions=1)
        with patch.object(sys, "argv", ["run_experiments.py"]):
            with patch.multiple(runner, ROOT=self.root, TIME_BUDGET_HOURS=2):
                with patch.object(runner, "capture", return_value=""):
                    with patch.object(runner, "make_experiment_plan", return_value=plan):
                        with patch.object(runner.time, "monotonic", return_value=100):
                            with patch.object(runner, "execute") as execute:
                                runner.main()
        self.assertEqual(execute.call_args.args[2], 100 + 2 * 3600)

    def test_deeper_sweep_settings_are_validated_before_starting(self):
        for depths in [[16, 15], [15, 15], [14], [101], ["15"]]:
            with self.subTest(depths=depths), patch.object(runner, "SWEEP_DEPTHS", depths):
                with self.assertRaises(ValueError):
                    runner.make_experiment_plan()

    def test_budget_stop_preserves_completed_results_and_starts_no_more_children(self):
        plan = runner.make_plan(depths=[4], threads=[1], hashes=[4], repetitions=2)
        output = self.root / "budget-test"
        clock = [100]
        bench_calls = []

        def fake_command(command, file, timeout):
            if command[0] == "make":
                binary = Path(next(arg[4:] for arg in command if arg.startswith("EXE=")))
                binary.write_bytes(b"fake binary, never executed")
            else:
                bench_calls.append(command)
                file.write(log_text())
                clock[0] = 111  # The first benchmark consumes the remaining budget.

        with patch.multiple(runner, ROOT=self.root, BUCKET_SIZES=[2]):
            with patch.object(runner, "record_metadata"):
                with patch.object(runner, "run_command", side_effect=fake_command):
                    with patch.object(runner.time, "monotonic", side_effect=lambda: clock[0]):
                        with self.assertRaises(runner.BudgetExpired):
                            runner.execute(plan, output, deadline=110)
        self.assertEqual(len(bench_calls), 1)
        self.assertTrue((output / "status.txt").read_text().startswith("budget-limited"))
        with (output / "runs.csv").open() as file:
            self.assertEqual(len(list(csv.DictReader(file))), 1)
        with (output / "positions.csv").open() as file:
            self.assertEqual(len(list(csv.DictReader(file))), 50)

    def test_invalid_settings_fail(self):
        with self.assertRaises(ValueError):
            runner.make_plan(caps=[2])
        for setting, value in [("BUCKET_SIZES", [3]), ("THREAD_COUNTS", [1, 1]),
                               ("DEPTHS", []), ("DEPTHS", [101]), ("REPETITIONS", 0),
                               ("TIMEOUT_SECONDS", float("nan")), ("HASH_SIZES_MB", [1])]:
            with self.subTest(setting=setting), patch.object(runner, setting, value):
                with self.assertRaises(ValueError):
                    runner.make_plan()
        with patch.object(runner.sys, "platform", "linux"), patch.object(runner, "HASH_SIZES_MB", [3]):
            with self.assertRaises(ValueError):
                runner.make_plan()

    def project(self):
        (self.root / "scripts").mkdir()
        (self.root / "src").mkdir()
        script = SCRIPT.read_text().replace("BUCKET_SIZES = [2]", "BUCKET_SIZES = [1, 2]")
        script = script.replace("DEPTHS = [15]", "DEPTHS = [4]")
        script = script.replace("THREAD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 192]", "THREAD_COUNTS = [1]")
        script = script.replace("HASH_SIZES_MB = [64]", "HASH_SIZES_MB = [4]")
        script = script.replace("REPETITIONS = 30", "REPETITIONS = 2")
        script = script.replace("SWEEP_DEPTHS = list(range(15, 31))", "SWEEP_DEPTHS = []")
        target = self.root / "scripts" / "run_experiments.py"
        target.write_text(script)
        return target

    def test_dry_run_launches_no_commands_or_creates_results(self):
        target = self.project()
        env = dict(os.environ, PATH=str(self.root / "empty-path"))
        result = subprocess.run([sys.executable, str(target), "--dry-run"], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 benchmark runs", result.stdout)
        self.assertFalse((self.root / "results").exists())

    def fake_project(self):
        target = self.project()
        commands = self.root / "fake-bin"
        commands.mkdir()
        fake_engine = """import sys
from pathlib import Path
import os
if os.environ.get("FAKE_FAIL_SECOND"):
    counter = Path(os.environ["FAKE_FAIL_SECOND"])
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if count == 2:
        print("deliberately incomplete fake engine output")
        sys.exit(7)
depth, threads, memory = map(int, sys.argv[2:])
bucket = @BUCKET@
cap = @CAP@
print(f'BENCH_CONFIG depth={depth} threads={threads} hash_mb={memory} bucket_size={bucket} entry_bytes=16 bucket_bytes={16*bucket} buckets={memory*1024*1024//(16*bucket)} cap_helpers={cap}')
print('BENCH_POSITION_HEADER,position,depth,wall_ns,cpu_ns,main_nodes,nodes,bestmove,score_type,score,raw_score,helper_max_started_iteration,helper_max_completed_iteration,helper_nodes_beyond_depth')
for i in range(1, 51): print(f'BENCH_POSITION,{i},{depth},1000000,1000000,10,{10*threads},e2e4,cp,20,20,0,0,0')
print('BENCH_TOTAL wall_ns=50000000 cpu_ns=50000000')
print(f'OVERALL: 50 ms {500*threads} nodes {10000*threads} nps')
"""
        engine = "#!" + sys.executable + "\n" + fake_engine
        make = f"""#!{sys.executable}
import sys
from pathlib import Path
if '--version' in sys.argv:
    print('FAKE make')
else:
    target = Path(next(a[4:] for a in sys.argv if a.startswith('EXE=')))
    bucket = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('BUCKET_SIZE='))
    cap = next(a.rsplit('=', 1)[1] for a in sys.argv if a.startswith('CPPFLAGS=-DBENCH_CAP_HELPERS='))
    target.write_text({engine!r}.replace('@BUCKET@', bucket).replace('@CAP@', cap))
    target.chmod(0o755)
"""
        git = f"""#!{sys.executable}
import sys
if 'rev-parse' in sys.argv: print('fake-commit')
"""
        for name, text in [("make", make), ("git", git), ("gcc", f"#!{sys.executable}\nprint('FAKE compiler')\n")]:
            file = commands / name
            file.write_text(text)
            file.chmod(0o755)
        env = dict(os.environ, PATH=str(commands), PYTHONDONTWRITEBYTECODE="1")
        return target, env

    def test_complete_workflow_with_fake_compiler_and_fake_engine(self):
        target, env = self.fake_project()
        result = subprocess.run([sys.executable, str(target)], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = next((self.root / "results" / "experiments").iterdir())
        self.assertTrue((output / "status.txt").read_text().startswith("complete"))
        with (output / "runs.csv").open() as file:
            runs = list(csv.DictReader(file))
        with (output / "positions.csv").open() as file:
            positions = list(csv.DictReader(file))
        self.assertEqual(len(runs), 4)
        self.assertEqual(len(positions), 200)
        self.assertEqual(len(list((output / "bin").iterdir())), 2)
        self.assertEqual({r["bucket_size"] for r in runs}, {"1", "2"})
        self.assertTrue(all((output / r["raw_log"]).is_file() for r in runs))
        self.assertTrue((output / "runner.py").is_file())
        self.assertIn("fake-commit", (output / "run-info.txt").read_text())

    def cap_project(self):
        target, env = self.fake_project()
        cap_script = SCRIPT.with_name("run_helper_cap.py")
        cap_target = target.with_name(cap_script.name)
        cap_target.write_text(cap_script.read_text())
        return cap_target, env

    def test_separate_cap_runner_dry_run_has_only_the_small_comparison(self):
        target, env = self.cap_project()
        result = subprocess.run([sys.executable, str(target), "--dry-run"], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("30 benchmark runs x 50 positions; 2 build(s).", result.stdout)
        builds = [line for line in result.stdout.splitlines() if line.startswith("make ")]
        self.assertEqual(len(builds), 2)
        self.assertTrue(any("-DBENCH_CAP_HELPERS=0" in line for line in builds))
        self.assertTrue(any("-DBENCH_CAP_HELPERS=1" in line for line in builds))
        commands = [line for line in result.stdout.splitlines() if line.startswith("run=")]
        self.assertEqual(len(commands), 30)
        for line in commands:
            self.assertIn("phase=helper-cap", line)
            self.assertEqual(line.split()[-4:-2], ["bench", "15"])
            self.assertIn(line.split()[-2], ["1", "32", "128"])
            self.assertEqual(line.split()[-1], "64")
        for workers in [1, 32, 128]:
            for cap in [0, 1]:
                self.assertEqual(sum(Path(shlex.split(line)[3]).name == f"weiss-b2-cap{cap}"
                                     and shlex.split(line)[-2] == str(workers) for line in commands), 5)
        self.assertFalse((self.root / "results").exists())

    def test_cap_runner_builds_both_variants_and_keeps_all_positions(self):
        target, env = self.cap_project()
        # This is a fake engine, so allow its 128-worker-labelled output on any laptop.
        target.write_text(target.read_text().replace("def main():",
                          "def main():\n    runner.os.sched_getaffinity = lambda _: set(range(192))"))
        result = subprocess.run([sys.executable, str(target)], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = next((self.root / "results" / "experiments").iterdir())
        self.assertTrue(output.name.startswith("helper-cap-"))
        with (output / "runs.csv").open() as file:
            runs = list(csv.DictReader(file))
        with (output / "positions.csv").open() as file:
            positions = list(csv.DictReader(file))
        self.assertEqual(len(runs), 30)
        self.assertEqual(len(positions), 1500)
        self.assertEqual(len(list((output / "bin").iterdir())), 2)
        self.assertEqual({r["phase"] for r in runs}, {"helper-cap"})
        self.assertEqual({r["cap_helpers"] for r in runs}, {"0", "1"})
        self.assertEqual({r["depth"] for r in runs}, {"15"})
        self.assertEqual({r["hash_mb"] for r in runs}, {"64"})
        self.assertEqual({r["threads"] for r in runs}, {"1", "32", "128"})
        for cap in ["0", "1"]:
            selected = [r for r in runs if r["cap_helpers"] == cap]
            self.assertEqual(len({r["binary_sha256"] for r in selected}), 1)
            self.assertTrue(all(f"weiss-b2-cap{cap} bench" in r["command"] for r in selected))
            self.assertTrue(all(len(r["command"].split()) == 5 for r in selected))
        self.assertEqual(len({r["binary_sha256"] for r in runs}), 2)
        self.assertEqual((output / "runner.py").read_text(), target.read_text())
        self.assertTrue((output / "run_experiments.py").is_file())
        self.assertTrue((output / "status.txt").read_text().startswith("complete"))

    def test_failed_run_keeps_prior_rows_and_failing_raw_log(self):
        target, env = self.fake_project()
        env["FAKE_FAIL_SECOND"] = str(self.root / "fake-counter")
        result = subprocess.run([sys.executable, str(target)], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        output = next((self.root / "results" / "experiments").iterdir())
        self.assertTrue((output / "status.txt").read_text().startswith("failed"))
        with (output / "runs.csv").open() as file:
            self.assertEqual(len(list(csv.DictReader(file))), 1)
        with (output / "positions.csv").open() as file:
            self.assertEqual(len(list(csv.DictReader(file))), 50)
        self.assertIn("deliberately incomplete", (output / "raw/run-0002.log").read_text())


if __name__ == "__main__":
    unittest.main()
