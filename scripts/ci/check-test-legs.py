#!/usr/bin/env python3
"""Verify CI test leg completeness and aggregate results into GITHUB_STEP_SUMMARY."""

import argparse
import json
import os
import re
import sys


def load_reports(reports_dir):
    """Recursively load all *.json test leg reports from reports_dir."""
    reports = []
    if not os.path.isdir(reports_dir):
        return reports
    for root, _, filenames in os.walk(reports_dir):
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                reports.append(json.load(fh))
    return reports


def resolve_expected_files(tracked_files, scope):
    """Determine the exact set of test files expected to have run."""
    scope_clean = scope.strip()
    if scope_clean == "all":
        return list(tracked_files)
    if scope_clean in ("", "none"):
        return []
    return [tok for tok in re.split(r"[\s,]+", scope_clean) if tok]


def evaluate_legs(expected_files, reports):
    """Pure verifier: return (ok, summary_markdown, error_messages)."""
    counts = {}
    leg_rows = []
    errors = []

    for report in reports:
        leg = report.get("leg", "unknown")
        entries = report.get("files", [])
        leg_tests = 0
        leg_failures = 0
        leg_errors = 0
        leg_skipped = 0
        leg_duration = 0.0

        for entry in entries:
            file_path = entry["file"]
            counts[file_path] = counts.get(file_path, 0) + 1
            t = int(entry.get("tests", 0))
            f = int(entry.get("failures", 0))
            e = int(entry.get("errors", 0))
            s = int(entry.get("skipped", 0))
            d = float(entry.get("duration_s", 0.0))
            leg_tests += t
            leg_failures += f
            leg_errors += e
            leg_skipped += s
            leg_duration += d
            if f > 0 or e > 0:
                errors.append(f"{leg}: {file_path} had failures={f}, errors={e}")

        leg_rows.append(
            {
                "leg": leg,
                "files": len(entries),
                "tests": leg_tests,
                "failures": leg_failures,
                "errors": leg_errors,
                "skipped": leg_skipped,
                "duration_s": round(leg_duration, 3),
            }
        )

    missing = [f for f in expected_files if counts.get(f, 0) == 0]
    duplicates = [f for f, count in sorted(counts.items()) if count > 1]

    if missing:
        errors.append(f"missed test files ({len(missing)}): {', '.join(missing)}")
    if duplicates:
        errors.append(f"duplicate test files across legs ({len(duplicates)}): {', '.join(duplicates)}")

    lines = [
        "### CI Test Legs Summary",
        "",
        "| Leg | Files | Tests | Failures | Errors | Skipped | Duration (s) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sorted(leg_rows, key=lambda r: r["leg"]):
        lines.append(
            f"| {row['leg']} | {row['files']} | {row['tests']} | "
            f"{row['failures']} | {row['errors']} | {row['skipped']} | {row['duration_s']:.3f} |"
        )
    if not leg_rows:
        lines.append("| (none) | 0 | 0 | 0 | 0 | 0 | 0.000 |")
    lines.append("")

    return len(errors) == 0, "\n".join(lines), errors


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify CI test leg completeness.")
    parser.add_argument("--scope", default="all", help="'all', 'none', or space-separated test files.")
    parser.add_argument("reports_dir", help="Directory containing leg JSON reports.")
    args = parser.parse_args(argv)

    tracked_files = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    expected_files = resolve_expected_files(tracked_files, args.scope)
    reports = load_reports(args.reports_dir)
    ok, summary_md, errors = evaluate_legs(expected_files, reports)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary_md + "\n")

    sys.stdout.write(summary_md + "\n")
    if not ok:
        for err in errors:
            sys.stderr.write(f"ERROR: {err}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
