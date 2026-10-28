"""Runner checks using synthetic logs and fake commands; never run Weiss."""

import csv
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_experiments.py"
spec = importlib.util.spec_from_file_location("runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def log_text(depth=4, threads=1, memory=4, bucket=2):
    lines = [f"BENCH_CONFIG depth={depth} threads={threads} hash_mb={memory} bucket_size={bucket} "
             f"entry_bytes=16 bucket_bytes={16*bucket} buckets={memory*1024*1024//(16*bucket)}",
             "BENCH_POSITION_HEADER," + ",".join(runner.POSITION_FIELDS)]
    for i in range(1, 51):
        score = "mate,1,30999" if i == 1 else "cp,-20,-20"
        lines.append(f"BENCH_POSITION,{i},{depth},1000000,1000000,10,{10*threads},e2e4,{score}")
    lines += ["BENCH_TOTAL wall_ns=50000000 cpu_ns=50000000",
              f"OVERALL: 50 ms {500*threads} nodes {10000*threads} nps"]
    return "\n".join(lines) + "\n"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = dict(bucket_size=2, depth=4, threads=1, hash_mb=4)
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

    def test_invalid_settings_fail(self):
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
        script = script.replace("DEPTHS = [12]", "DEPTHS = [4]")
        script = script.replace("THREAD_COUNTS = [1, 2, 4, 8]", "THREAD_COUNTS = [1]")
        script = script.replace("HASH_SIZES_MB = [64]", "HASH_SIZES_MB = [4]")
        script = script.replace("REPETITIONS = 5", "REPETITIONS = 2")
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
bucket = int(Path(sys.argv[0]).name.rsplit('b', 1)[1])
print(f'BENCH_CONFIG depth={depth} threads={threads} hash_mb={memory} bucket_size={bucket} entry_bytes=16 bucket_bytes={16*bucket} buckets={memory*1024*1024//(16*bucket)}')
print('BENCH_POSITION_HEADER,position,depth,wall_ns,cpu_ns,main_nodes,nodes,bestmove,score_type,score,raw_score')
for i in range(1, 51): print(f'BENCH_POSITION,{i},{depth},1000000,1000000,10,{10*threads},e2e4,cp,20,20')
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
    target.write_text({engine!r})
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
