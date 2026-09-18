#!/usr/bin/env python3
"""Compute the package cache namespace from build-affecting inputs only.

Hash whole shared build recipes, artifact/cache actions, and build helpers.
Caller publish/test workflows only orchestrate these shared inputs and are
excluded, as are verification, publication, and documentation files. The build
image digest and upstream trees are supplied separately by the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

BUILD_INPUTS = (
    Path(".github/workflows/build-recipe.yaml"),
    Path(".github/workflows/build-standalone.yaml"),
    Path(".github/actions/package-paths/action.yaml"),
    Path(".github/actions/package-artifacts/action.yaml"),
    Path(".github/actions/restore-package/action.yaml"),
    Path("scripts/package_catalog.json"),
    Path("scripts/package_catalog.py"),
    Path("scripts/package_build_policy.py"),
    Path("scripts/prepare_package_build.py"),
    Path("scripts/plan_builds.py"),
)
JOB_PATTERN = re.compile(r"^  ([A-Za-z0-9_-]+):$")


def job_text(workflow: str, job: str) -> str:
    """Extract a job for workflow contract tests; not used for cache hashing."""
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


def build_surface(workflow: Path) -> bytes:
    """Read required inputs, framing paths and bytes to preserve boundaries."""
    parts = []
    for relative in BUILD_INPUTS:
        for value in (
            relative.as_posix().encode("utf-8"),
            (workflow / relative).read_bytes(),
        ):
            parts.extend((str(len(value)).encode("ascii"), b":", value))
    return b"".join(parts)


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
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    digest.update(build_surface(workflow))
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
