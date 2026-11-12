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
        plan = runner.make_plan(extra_runs=[{"name": "capped", "cap_helpers": 1}], repetitions=1)
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

    def test_affinity_preflight_is_deduplicated_by_candidate_build_and_threads(self):
        plan = [
            {"condition": "candidate", "cpu_binding": "physical", "bucket_size": 2,
             "cap_helpers": 0, "threads": 8},
            {"condition": "candidate", "cpu_binding": "physical", "bucket_size": 2,
             "cap_helpers": 0, "threads": 8},
            {"condition": "candidate", "cpu_binding": "physical", "bucket_size": 2,
             "cap_helpers": 0, "threads": 32},
            {"condition": "baseline", "cpu_binding": "physical", "bucket_size": 2,
             "cap_helpers": 0, "threads": 8},
            {"condition": "candidate", "cpu_binding": "none", "bucket_size": 2,
             "cap_helpers": 0, "threads": 8},
        ]
        self.assertEqual(runner.affinity_check_specs(plan),
                         [(2, 0, 8), (2, 0, 32)])

    def test_affinity_parser_checks_cpu_mask_and_running_cpu(self):
        cpus = [4, 8]
        self.log.write_text(
            "AFFINITY_THREAD thread=0 expected_cpu=4 running_cpu=4 allowed_count=1 "
            "allowed_cpus=4 status=pass\n"
            "AFFINITY_THREAD thread=1 expected_cpu=8 running_cpu=8 allowed_count=1 "
            "allowed_cpus=8 status=pass\n"
            "AFFINITY_RESULT threads=2 status=pass\n")
        rows, passed = runner.parse_affinity_check(
            self.log, check_id="sample", bucket_size=2, cap_helpers=0, threads=2,
            physical_cpu_ids=cpus)
        self.assertTrue(passed)
        self.assertEqual([row["status"] for row in rows], ["pass", "pass"])
        self.assertEqual([row["running_cpu"] for row in rows], cpus)

        self.log.write_text(self.log.read_text().replace("allowed_cpus=8", "allowed_cpus=8,9"))
        rows, passed = runner.parse_affinity_check(
            self.log, check_id="sample", bucket_size=2, cap_helpers=0, threads=2,
            physical_cpu_ids=cpus)
        self.assertFalse(passed)
        self.assertEqual(rows[1]["status"], "fail")

    def test_affinity_preflight_runs_before_measurement_and_saves_worker_rows(self):
        output = self.root / "affinity-run"
        (output / "raw").mkdir(parents=True)
        binary = self.root / "weiss-candidate"
        plan = [{"condition": "candidate", "cpu_binding": "physical",
                 "bucket_size": 2, "cap_helpers": 0, "threads": 2}]
        binaries = {("candidate", 2, 0): (binary, "unused-hash")}
        calls = []

        def fake_command(command, log, timeout, env=None, check=True):
            calls.append((command, timeout, env, check))
            self.assertEqual(env["WEISS_CPU_LIST"], "4,8")
            log.write(
                "AFFINITY_THREAD thread=0 expected_cpu=4 running_cpu=4 allowed_count=1 "
                "allowed_cpus=4 status=pass\n"
                "AFFINITY_THREAD thread=1 expected_cpu=8 running_cpu=8 allowed_count=1 "
                "allowed_cpus=8 status=pass\n"
                "AFFINITY_RESULT threads=2 status=pass\n")
            return 0

        with patch.object(runner, "CHECK_THREAD_AFFINITY", True), patch.object(
                runner, "run_command", side_effect=fake_command):
            checked = runner.run_affinity_checks(plan, output, binaries, [4, 8], deadline=None)

        self.assertEqual(checked, 2)
        self.assertEqual(calls[0][0], [str(binary), "affinity-check", "2"])
        self.assertFalse(calls[0][3])  # Collect output even on failure for the CSV/log.
        with (output / "affinity-checks.csv").open(newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([row["status"] for row in rows], ["pass", "pass"])
        self.assertEqual([row["expected_cpu"] for row in rows], ["4", "8"])

    def test_plan_always_includes_defaults_and_extras_only_override_named_settings(self):
        extras = [{"name": "threads-2", "threads": 2}, {"name": "depth-5", "depth": 5}]
        a = runner.make_plan(extra_runs=extras, repetitions=2)
        b = runner.make_plan(extra_runs=extras, repetitions=2)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 6)
        for repetition in [1, 2]:
            group = [r for r in a if r["repetition"] == repetition]
            self.assertEqual({r["config_name"] for r in group},
                             {"weiss-default", "threads-2", "depth-5"})
            default = next(r for r in group if r["config_name"] == "weiss-default")
            threads = next(r for r in group if r["config_name"] == "threads-2")
            depth = next(r for r in group if r["config_name"] == "depth-5")
            self.assertEqual(threads["threads"], 2)
            self.assertEqual(threads["hash_mb"], default["hash_mb"])
            self.assertEqual(depth["depth"], 5)
            self.assertEqual(depth["threads"], default["threads"])

    def test_every_candidate_has_an_equal_work_pinned_baseline_pair(self):
        plan = runner.make_experiment_plan()
        self.assertEqual([r["run"] for r in plan], list(range(1, len(plan) + 1)))
        self.assertEqual(len(plan) % 2, 0)
        comparisons = {}
        for row in plan:
            comparisons.setdefault(row["comparison_id"], {})[row["condition"]] = row
        self.assertTrue(all(set(pair) == {"baseline", "candidate"} for pair in comparisons.values()))
        for pair in comparisons.values():
            baseline, candidate = pair["baseline"], pair["candidate"]
            for field in ("bucket_size", "depth", "threads", "hash_mb", "cap_helpers",
                          "rotate_root_moves", "cpu_binding", "repetition"):
                self.assertEqual(baseline[field], candidate[field])
        self.assertEqual({row["config_name"] for row in plan},
                         {"weiss-default", *(extra["name"] for extra in runner.EXTRA_RUNS)})
        self.assertEqual({row["phase"] for row in plan}, {"default", "extra"})

    def test_time_budget_applies_without_a_special_flag(self):
        with patch.object(sys, "argv", ["run_experiments.py"]):
            with patch.multiple(runner, ROOT=self.root, TIME_BUDGET_HOURS=2,
                                BASELINE_GIT_REF=None, SELFPLAY_ENABLED=False,
                                WEISS_DEFAULT=dict(runner.WEISS_DEFAULT, depth=4, hash_mb=4),
                                EXTRA_RUNS=[], REPETITIONS=1):
                with patch.object(runner, "capture", return_value=""):
                    with patch.object(runner.time, "monotonic", return_value=100):
                        with patch.object(runner, "execute") as execute:
                            runner.main()
        self.assertEqual(execute.call_args.args[2], 100 + 2 * 3600)

    def test_budget_stop_preserves_completed_results_and_starts_no_more_children(self):
        with patch.multiple(runner, BASELINE_GIT_REF=None, SELFPLAY_ENABLED=False,
                            WEISS_DEFAULT=dict(runner.WEISS_DEFAULT, depth=4, hash_mb=4),
                            EXTRA_RUNS=[], REPETITIONS=2):
            plan = runner.make_experiment_plan()
        output = self.root / "budget-test"
        clock = [100]
        bench_calls = []

        def fake_command(command, file, timeout, env=None):
            if command[0] == "make":
                binary = Path(next(arg[4:] for arg in command if arg.startswith("EXE=")))
                binary.write_bytes(b"fake binary, never executed")
            else:
                bench_calls.append(command)
                file.write(log_text())
                clock[0] = 111  # The first benchmark consumes the remaining budget.

        with patch.multiple(runner, ROOT=self.root, BASELINE_GIT_REF=None,
                            SELFPLAY_ENABLED=False):
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
        bad_extras = [[{"name": "bad-bucket", "bucket_size": 3}],
                      [{"name": "too-many-threads", "threads": 2049}],
                      [{"name": "too-deep", "depth": 101}],
                      [{"name": "duplicate-default", "threads": runner.WEISS_DEFAULT["threads"]}],
                      [{"name": "same", "threads": 2}, {"name": "same", "threads": 4}]]
        for extras in bad_extras:
            with self.subTest(extras=extras), self.assertRaises(ValueError):
                runner.make_plan(extra_runs=extras)
        with patch.object(runner.sys, "platform", "linux"), patch.object(
                runner, "WEISS_DEFAULT", dict(runner.WEISS_DEFAULT, hash_mb=3)):
            with self.assertRaises(ValueError):
                runner.make_plan()
        with patch.object(runner, "REPETITIONS", 0):
            with self.assertRaises(ValueError):
                runner.make_plan()
        with patch.object(runner, "TIMEOUT_SECONDS", float("nan")):
            with self.assertRaises(ValueError):
                runner.make_plan()

    def project(self):
        (self.root / "scripts").mkdir()
        (self.root / "src").mkdir()
        script = SCRIPT.read_text().replace('"depth": 14', '"depth": 4', 1)
        script = script.replace('"hash_mb": 32', '"hash_mb": 4', 1)
        start = script.index("EXTRA_RUNS = [")
        end = script.index("\n]", start) + 2
        script = script[:start] + 'EXTRA_RUNS = [{"name": "bucket-one", "bucket_size": 1}]' + script[end:]
        script = script.replace("REPETITIONS = 5", "REPETITIONS = 2")
        script = script.replace("BASELINE_GIT_REF = \"c55272499a460acdb7bd057ad55d495b65faaefd\"",
                                "BASELINE_GIT_REF = None")
        script = script.replace("SELFPLAY_ENABLED = True", "SELFPLAY_ENABLED = False")
        target = self.root / "scripts" / "run_experiments.py"
        target.write_text(script)
        return target

    def test_dry_run_launches_no_commands_or_creates_results(self):
        target = self.project()
        env = dict(os.environ, PATH=str(self.root / "empty-path"))
        result = subprocess.run([sys.executable, str(target), "--dry-run"], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 paired comparisons (8 benchmark runs)", result.stdout)
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
        self.assertEqual(len(runs), 8)
        self.assertEqual(len(positions), 400)
        self.assertEqual(len(list((output / "bin").rglob("weiss-*"))), 4)
        self.assertEqual({r["bucket_size"] for r in runs}, {"1", "2"})
        self.assertEqual({r["condition"] for r in runs}, {"baseline", "candidate"})
        with (output / "comparisons.csv").open() as file:
            self.assertEqual(len(list(csv.DictReader(file))), 4)
        with (output / "position-comparisons.csv").open() as file:
            paired_positions = list(csv.DictReader(file))
        self.assertEqual(len(paired_positions), 200)
        self.assertTrue(all(row["same_move"] == "1" for row in paired_positions))
        self.assertTrue(all((output / r["raw_log"]).is_file() for r in runs))
        self.assertTrue((output / "runner.py").is_file())
        self.assertIn("fake-commit", (output / "run-info.txt").read_text())

    def test_cap_and_rotation_modes_are_editable_in_the_regular_plan(self):
        plan = runner.make_experiment_plan(extra_runs=[
            {"name": "capped-rotated", "cap_helpers": 1, "rotate_root_moves": 1}],
            repetitions=1)
        self.assertEqual(len(plan), 4)
        self.assertEqual({row["cap_helpers"] for row in plan}, {0, 1})
        self.assertEqual({row["rotate_root_moves"] for row in plan}, {0, 1})
        self.assertTrue(all(row["condition"] in {"baseline", "candidate"} for row in plan))
        default = [row for row in plan if row["config_name"] == "weiss-default"]
        self.assertEqual({row["phase"] for row in default}, {"default"})

    def test_final_selfplay_smoke_records_color_swapped_games(self):
        output = self.root / "selfplay"
        output.mkdir()
        pgn = (
            '[White "Weiss candidate"]\n[Black "Weiss baseline"]\n[Result "1-0"]\n\n*\n\n'
            '[White "Weiss baseline"]\n[Black "Weiss candidate"]\n[Result "1/2-1/2"]\n\n*\n\n'
            '[White "Weiss candidate"]\n[Black "Weiss baseline"]\n[Result "1/2-1/2"]\n\n*\n\n'
            '[White "Weiss baseline"]\n[Black "Weiss candidate"]\n[Result "1-0"]\n\n*\n\n'
        )

        def fake_run(command, log, timeout, env=None):
            pgn_file = command[command.index("-pgnout") + 1].removeprefix("file=")
            Path(pgn_file).write_text(pgn)
            log.write("fake Fastchess completed four games\n")

        with patch.object(runner.shutil, "which", return_value="/fake/fastchess"):
            with patch.object(runner, "run_command", side_effect=fake_run):
                result = runner.run_selfplay_smoke(
                    output, self.root / "baseline", self.root / "candidate", None)
        self.assertEqual(result, "completed 4 games; smoke test only")
        self.assertTrue((output / "selfplay-command.txt").read_text().startswith("/fake/fastchess "))
        command = (output / "selfplay-command.txt").read_text()
        self.assertIn("-rounds 2 -repeat", command)
        self.assertIn(f"-pgnout file={output / 'selfplay.pgn'} append=false", command)
        with (output / "selfplay-summary.csv").open() as file:
            summary = next(csv.DictReader(file))
        self.assertEqual((summary["candidate_wins"], summary["draws"],
                          summary["candidate_losses"]), ("1", "2", "1"))
        self.assertIn("complete: 4 games", (output / "selfplay-status.txt").read_text())

    def test_fastchess_path_is_discovered(self):
        with patch.object(runner.shutil, "which", return_value="/home/luott/.local/bin/fastchess"):
            self.assertEqual(runner.find_fastchess(), "/home/luott/.local/bin/fastchess")

    def test_required_fastchess_is_checked_before_any_benchmark_starts(self):
        with patch.object(sys, "argv", ["run_experiments.py"]):
            with patch.multiple(runner, ROOT=self.root, SELFPLAY_ENABLED=True,
                                SELFPLAY_REQUIRED=True, BASELINE_GIT_REF=None,
                                WEISS_DEFAULT=dict(runner.WEISS_DEFAULT, depth=4, hash_mb=4),
                                EXTRA_RUNS=[], REPETITIONS=1):
                with patch.object(runner, "capture", return_value=""):
                    with patch.object(runner.shutil, "which", return_value=None):
                        with patch.object(runner, "execute") as execute:
                            with self.assertRaisesRegex(ValueError, "fastchess"):
                                runner.main()
        execute.assert_not_called()

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
