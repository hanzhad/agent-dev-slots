#!/usr/bin/env python3
"""Sharded parallel unittest runner for CI and local verification."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import time

TIMINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-timings.json")
_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)

_SINGLE_FILE_RUNNER = """
import json, os, sys, time, unittest

file_path, result_path = sys.argv[1], sys.argv[2]
abs_path = os.path.abspath(file_path)
start_dir = os.path.dirname(abs_path)
if start_dir not in sys.path:
    sys.path.insert(0, start_dir)

t0 = time.monotonic()
loader = unittest.TestLoader()
suite = loader.discover(start_dir=start_dir, pattern=os.path.basename(abs_path), top_level_dir=start_dir)
runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=1)
result = runner.run(suite)
duration_s = round(time.monotonic() - t0, 3)

payload = {
    "tests": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "duration_s": duration_s,
}
with open(result_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def discover_test_files(tests_dir="tests"):
    """Return sorted top-level tests/test_*.py paths, excluding tests/docker/."""
    if not os.path.isdir(tests_dir):
        return []
    files = [
        os.path.join(tests_dir, name)
        for name in sorted(os.listdir(tests_dir))
        if name.startswith("test_")
        and name.endswith(".py")
        and os.path.isfile(os.path.join(tests_dir, name))
    ]
    return files


def read_files_from(path):
    """Read non-empty test file paths from a manifest file."""
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def parse_shard(spec):
    """Parse 'i/N' into (i, N) with 1 <= i <= N."""
    if "/" not in spec:
        raise ValueError(f"invalid shard {spec!r}: expected i/N")
    left, right = spec.split("/", 1)
    idx = int(left)
    total = int(right)
    if total < 1 or idx < 1 or idx > total:
        raise ValueError(f"shard {spec!r} out of bounds (1 <= i <= N, N >= 1)")
    return idx, total


def _extract_timing_weight(entry):
    if isinstance(entry, (int, float)) and not isinstance(entry, bool) and entry > 0:
        return float(entry)
    if isinstance(entry, dict):
        for key in ("duration_s", "weight", "duration", "seconds", "tests"):
            val = entry.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return float(val)
    return None


def load_timings(path=TIMINGS_FILE):
    """Load test timing entries from a JSON file; return {} when absent or invalid."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
        return raw["files"]
    if isinstance(raw, dict):
        return raw
    return {}


def _lookup_timing_weight(timings, path):
    if not timings:
        return None
    norm = path.replace("\\", "/")
    candidates = [norm]
    if norm.startswith("./"):
        candidates.append(norm[2:])
    if "/tests/" in norm:
        candidates.append("tests/" + norm.rsplit("/tests/", 1)[1])
    candidates.append(os.path.basename(norm))
    for key in candidates:
        if key in timings:
            weight = _extract_timing_weight(timings[key])
            if weight is not None:
                return weight
    return None


def _count_test_methods(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return len(_TEST_DEF_RE.findall(source))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def _file_weight(path, timings):
    measured = _lookup_timing_weight(timings, path)
    if measured is not None:
        return measured
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return 0.0
    test_count = _count_test_methods(source)
    if test_count > 0:
        return float(test_count)
    return float(source.count("\n") + (1 if source and not source.endswith("\n") else 0)) * 0.01


def select_shard(paths, shard_index, shard_total, timings=None):
    """Distribute paths across shard_total buckets deterministically by timing weight."""
    if timings is None:
        timings = load_timings()
    weighted = sorted(((-_file_weight(p, timings), p) for p in paths))
    buckets = [[] for _ in range(shard_total)]
    loads = [0.0] * shard_total
    for neg_weight, path in weighted:
        target = min(range(shard_total), key=lambda b: (loads[b], len(buckets[b]), b))
        buckets[target].append(path)
        loads[target] += -neg_weight
    return buckets[shard_index - 1]


def _run_single_file(file_path, port_slot):
    env = dict(os.environ)
    env["DEV_CONTAINER_PORT_SLOT"] = str(port_slot)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="test-result-", delete=False
    ) as tmp:
        result_path = tmp.name

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SINGLE_FILE_RUNNER, file_path, result_path],
            env=env,
            capture_output=True,
            text=True,
        )
        duration_s = round(time.monotonic() - t0, 3)
        entry = None
        if os.path.isfile(result_path) and os.path.getsize(result_path) > 0:
            with open(result_path, encoding="utf-8") as fh:
                entry = json.load(fh)
        if entry is None:
            entry = {
                "tests": 0,
                "failures": 0,
                "errors": 1,
                "skipped": 0,
                "duration_s": duration_s,
            }
        entry["file"] = file_path
        if proc.returncode != 0 or entry["failures"] > 0 or entry["errors"] > 0:
            if proc.stdout:
                sys.stderr.write(proc.stdout)
            if proc.stderr:
                sys.stderr.write(proc.stderr)
        return entry
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass


def run_test_files(paths, workers):
    """Run test files across a worker pool with isolated DEV_CONTAINER_PORT_SLOT."""
    if not paths:
        return []
    slot_queue = queue.Queue()
    for slot_id in range(workers):
        slot_queue.put(slot_id)

    def task(path):
        slot_id = slot_queue.get()
        try:
            return _run_single_file(path, slot_id)
        finally:
            slot_queue.put(slot_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(task, paths))


def build_report(leg, entries):
    """Build the JSON report dictionary for a leg."""
    return {
        "leg": leg,
        "files": [
            {
                "file": e["file"],
                "tests": e["tests"],
                "failures": e["failures"],
                "errors": e["errors"],
                "skipped": e["skipped"],
                "duration_s": e["duration_s"],
            }
            for e in entries
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run sharded unittest files in parallel.")
    parser.add_argument("--shard", help="Shard specification i/N (1-based).")
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker count.")
    parser.add_argument("--leg", default="default", help="Leg name for the report.")
    parser.add_argument("--files-from", help="Path to newline-separated list of test files.")
    parser.add_argument("--report", help="Output JSON report path.")
    args = parser.parse_args(argv)

    if args.workers < 1:
        parser.error("--workers must be >= 1")

    shard_spec = None
    if args.shard is not None:
        try:
            shard_spec = parse_shard(args.shard)
        except ValueError as exc:
            parser.error(str(exc))

    if args.files_from is not None:
        paths = read_files_from(args.files_from)
    else:
        paths = discover_test_files()

    if shard_spec is not None:
        paths = select_shard(paths, shard_spec[0], shard_spec[1])

    entries = run_test_files(paths, args.workers)
    report = build_report(args.leg, entries)

    if args.report:
        report_dir = os.path.dirname(os.path.abspath(args.report))
        os.makedirs(report_dir, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)

    total_tests = sum(e["tests"] for e in entries)
    total_failures = sum(e["failures"] for e in entries)
    total_errors = sum(e["errors"] for e in entries)
    total_skipped = sum(e["skipped"] for e in entries)
    print(
        f"[{args.leg}] files={len(entries)} tests={total_tests} "
        f"failures={total_failures} errors={total_errors} skipped={total_skipped}"
    )

    if total_tests == 0 or total_failures > 0 or total_errors > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
