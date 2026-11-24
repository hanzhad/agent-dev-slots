#!/usr/bin/env python3
"""Select CI execution plan from changed files and EVENT_NAME (fail-closed)."""

import json
import math
import os
import sys

SHARD_COUNT = 6
FULL_MATRIX = [f"shard-{i}" for i in range(1, SHARD_COUNT + 1)]

FULL_RUN_PREFIXES = (
    ".github/",
    "scripts/ci/",
    "bin/",
    "lib/",
    "templates/",
    "image/",
)
FULL_RUN_EXACT = ("tests/harness.py", "__fail_closed__")


def _is_full_run_path(path):
    if path in FULL_RUN_EXACT:
        return True
    if any(path.startswith(prefix) for prefix in FULL_RUN_PREFIXES):
        return True
    base = os.path.basename(path)
    if base.startswith("Dockerfile") or base.startswith("compose") or base.startswith("docker-compose"):
        return True
    return False


def _is_docs_or_readme(path):
    base = os.path.basename(path)
    return path.startswith("docs/") or base.startswith("README")


def _is_docker_test(path):
    return path.startswith("tests/docker/")


def _is_top_level_unit_test(path):
    return (
        path.startswith("tests/test_")
        and path.endswith(".py")
        and "/" not in path[len("tests/"):]
    )


def select_plan(changed_files, event_name, exists_fn=None):
    """Pure planner: return (all_flag, matrix_list, run_docker, scope_str)."""
    files = [line.strip() for line in changed_files if line.strip()]

    if event_name in ("push", "workflow_dispatch"):
        return True, list(FULL_MATRIX), True, "all"

    if not files:
        return True, list(FULL_MATRIX), True, "all"

    run_docker = False
    selected_tests = []

    for path in files:
        if _is_full_run_path(path):
            return True, list(FULL_MATRIX), True, "all"
        if _is_docker_test(path):
            run_docker = True
            continue
        if _is_top_level_unit_test(path):
            if exists_fn is not None and not exists_fn(path):
                continue
            if path not in selected_tests:
                selected_tests.append(path)
            continue
        if _is_docs_or_readme(path):
            continue
        # Fail closed on any unrecognized path outside docs/README/tests.
        return True, list(FULL_MATRIX), True, "all"

    if selected_tests:
        if len(selected_tests) <= 2:
            matrix = ["selected"]
        else:
            leg_count = min(SHARD_COUNT, math.ceil(len(selected_tests) / 2))
            matrix = [f"selected-{i}" for i in range(1, leg_count + 1)]
        return False, matrix, run_docker, " ".join(selected_tests)
    return False, [], run_docker, "none"


def format_outputs(all_flag, matrix_list, run_docker, scope_str):
    """Format plan outputs as key=value lines for GITHUB_OUTPUT or stdout."""
    lines = [
        f"all={'true' if all_flag else 'false'}",
        f"matrix={json.dumps(matrix_list)}",
        f"run_docker={'true' if run_docker else 'false'}",
        f"scope={scope_str}",
    ]
    return "\n".join(lines) + "\n"


def main():
    event_name = os.environ.get("EVENT_NAME", "").strip()
    changed_files = sys.stdin.read().splitlines()
    all_flag, matrix_list, run_docker, scope_str = select_plan(changed_files, event_name, exists_fn=os.path.isfile)
    rendered = format_outputs(all_flag, matrix_list, run_docker, scope_str)

    github_output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
