#!/usr/bin/env python3
"""Compute the package cache namespace from build-affecting inputs only.

Inputs that cannot change package bytes are deliberately excluded: edits to
verification or publication jobs must not invalidate package caches. The
workflow surface is the text of the build and build-extra jobs plus the
restore-package composite action, dependency mapping, and package build policy
and adaptations, read from the checked-out repository.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

WORKFLOW_RELATIVE = Path(".github/workflows/publish.yaml")
COMPOSITE_RELATIVE = Path(".github/actions/restore-package/action.yaml")
DEPENDENCIES_RELATIVE = Path("scripts/package_dependencies.sh")
POLICY_RELATIVES = (
    Path("scripts/package_build_policy.json"),
    Path("scripts/package_build_policy.py"),
    Path("scripts/prepare_package_build.py"),
)
JOB_PATTERN = re.compile(r"^  ([A-Za-z0-9_-]+):$")
RECIPE_JOBS = ("build", "build-extra")


def job_text(workflow: str, job: str) -> str:
    """Return the text of a two-space-indented job block from workflow YAML."""
    lines = workflow.splitlines()
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        match = JOB_PATTERN.fullmatch(line)
        if match is None:
            continue
        if start is None:
            if match[1] == job:
                start = index
            continue
        if end is None:
            end = index
            break
    if start is None:
        raise ValueError(f"workflow job not found: {job}")
    if end is None:
        end = len(lines)
    return "\n".join(lines[start:end]) + "\n"


def build_surface(workflow: Path) -> str:
    text = (workflow / WORKFLOW_RELATIVE).read_text(encoding="utf-8")
    parts = [job_text(text, job) for job in RECIPE_JOBS]
    parts.append((workflow / COMPOSITE_RELATIVE).read_text(encoding="utf-8"))
    parts.append((workflow / DEPENDENCIES_RELATIVE).read_text(encoding="utf-8"))
    for relative in POLICY_RELATIVES:
        parts.append((workflow / relative).read_text(encoding="utf-8"))
    return "".join(parts)


def namespace(
    build_image: str,
    patch_tree: str,
    shared_build_inputs: str,
    data_tree: str,
    workflow: Path,
) -> str:
    digest = hashlib.sha256()
    for value in (
        build_image,
        patch_tree,
        shared_build_inputs,
        data_tree,
        build_surface(workflow),
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-image", required=True)
    parser.add_argument("--patch-tree", required=True)
    parser.add_argument("--shared-build-inputs", required=True)
    parser.add_argument("--data-tree", required=True)
    parser.add_argument("--workflow", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        print(
            namespace(
                args.build_image,
                args.patch_tree,
                args.shared_build_inputs,
                args.data_tree,
                args.workflow,
            )
        )
    except (OSError, ValueError) as error:
        print(f"cache_namespace: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
