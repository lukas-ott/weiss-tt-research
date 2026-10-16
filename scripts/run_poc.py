#!/usr/bin/env python3
"""Build and benchmark the two proof-of-concept TT layouts."""

import argparse
import csv
import hashlib
import platform
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VARIANTS = (("direct", 1), ("two-way", 2))
OVERALL = re.compile(r"^OVERALL:\s+(\d+)\s+ms\s+(\d+)\s+nodes\s+(\d+)\s+nps\s*$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_output(*args):
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def benchmark_result(path):
    matches = []
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            match = OVERALL.fullmatch(line.strip())
            if match:
                matches.append(tuple(map(int, match.groups())))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one OVERALL line in {path}, found {len(matches)}")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=12)
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--hash-mb", type=int, nargs="+", default=[64])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--skip-build", action="store_true", help="Use existing binaries")
    parser.add_argument("--output-dir", type=Path, help="New directory for this run")
    args = parser.parse_args()

    if args.depth < 1 or args.repetitions < 1 or any(n < 1 for n in args.threads):
        parser.error("depth, repetitions, and thread counts must be positive")
    if any(mb < 2 for mb in args.hash_mb):
        parser.error("hash sizes must be at least 2 MB")

    source_status = git_output("status", "--short")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = args.output_dir or ROOT / "results" / "poc" / "runs" / run_id
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir()

    binaries = {}
    build_commands = []
    for variant, bucket_size in VARIANTS:
        binary = ROOT / "src" / f"weiss-{variant}"
        if not args.skip_build:
            command = [
                "make", "-C", "src", "basic", f"BUCKET_SIZE={bucket_size}",
                f"EXE={binary.name}",
            ]
            build_commands.append(shlex.join(command))
            build_log = output_dir / f"build-{variant}.log"
            with build_log.open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f"Build failed; see {build_log}")
        if not binary.is_file():
            raise FileNotFoundError(f"Missing binary: {binary}")
        binaries[variant] = (binary, bucket_size, sha256(binary))

    metadata = output_dir / "run-info.txt"
    metadata.write_text(
        f"started_utc: {utc_now()}\n"
        f"source_commit: {git_output('rev-parse', 'HEAD')}\n"
        f"source_status_before_run:\n{source_status or '(clean)'}\n"
        f"platform: {platform.platform()}\n"
        f"machine: {platform.machine()}\n"
        f"python: {sys.version.split()[0]}\n"
        f"runner_command: {shlex.join(sys.argv)}\n"
        f"build_commands: {build_commands if build_commands else '(skipped)'}\n"
        + "".join(
            f"{variant}_sha256: {binaries[variant][2]}\n" for variant, _ in VARIANTS
        ),
        encoding="utf-8",
    )

    csv_path = output_dir / "runs.csv"
    fields = [
        "started_utc", "variant", "bucket_size", "depth", "threads", "hash_mb",
        "repetition", "elapsed_ms", "nodes", "nps", "binary_sha256", "raw_log",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        output.flush()
        for repetition in range(1, args.repetitions + 1):
            for hash_mb in args.hash_mb:
                for threads in args.threads:
                    variants = VARIANTS if repetition % 2 else reversed(VARIANTS)
                    for variant, _ in variants:
                        binary, bucket_size, binary_hash = binaries[variant]
                        command = [str(binary), "bench", str(args.depth), str(threads), str(hash_mb)]
                        raw_log = raw_dir / (
                            f"{variant}-d{args.depth}-t{threads}-h{hash_mb}-r{repetition}.log"
                        )
                        started = utc_now()
                        with raw_log.open("w", encoding="utf-8") as log:
                            result = subprocess.run(
                                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
                            )
                        if result.returncode:
                            raise RuntimeError(f"Benchmark failed; see {raw_log}")
                        elapsed_ms, nodes, nps = benchmark_result(raw_log)
                        writer.writerow({
                            "started_utc": started,
                            "variant": variant,
                            "bucket_size": bucket_size,
                            "depth": args.depth,
                            "threads": threads,
                            "hash_mb": hash_mb,
                            "repetition": repetition,
                            "elapsed_ms": elapsed_ms,
                            "nodes": nodes,
                            "nps": nps,
                            "binary_sha256": binary_hash,
                            "raw_log": raw_log.relative_to(output_dir),
                        })
                        output.flush()
                        print(
                            f"{variant:7} t={threads} hash={hash_mb} MB "
                            f"r={repetition}: {elapsed_ms} ms, {nodes} nodes"
                        )

    print(f"Results: {csv_path}")


if __name__ == "__main__":
    main()
