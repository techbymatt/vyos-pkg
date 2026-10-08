"""Small, dependency-free IO helpers shared by the workflow entry points."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path


def canonical_json(value: object) -> bytes:
    """Encode deterministic UTF-8 JSON without a trailing newline."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def command_output(
    arguments: list[str],
    cwd: Path | None = None,
    *,
    env: dict | None = None,
    input_text: str | None = None,
    timeout: float = 120,
    strip: bool = True,
) -> str:
    """Run a bounded, noninteractive command, preserving stderr on failure."""
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
    ).stdout
    return result.strip() if strip else result


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a file only after its complete contents have been written.

    Resolve existing symlinks so checked edits update their target, not the link.
    Keep executable bits when replacing existing build scripts.
    """
    path = path.resolve()
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def output_lines(result: dict) -> Iterator[str]:
    """Encode single-line GITHUB_OUTPUT assignments without permitting injection."""
    for name, value in result.items():
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) is None:
            raise ValueError(f"invalid output name: {name!r}")
        encoded = (
            value
            if isinstance(value, str)
            else json.dumps(value, separators=(",", ":"))
        )
        if "\n" in encoded or "\r" in encoded:
            raise ValueError(f"output {name!r} would span multiple lines")
        yield f"{name}={encoded}"


def positive_int(value: str) -> int:
    """Argparse type for a positive concurrency limit."""
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("jobs must be positive")
    return count
