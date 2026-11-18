"""Retry transient Git transport failures without changing source refs or Git config."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

RETRY_DELAYS = (5, 15)
TRANSIENT_FAILURE = re.compile(
    r"^(?:fatal|error|ssh|kex_exchange_identification): .*(?:"
    r"could not resolve (?:host(?:name)?|proxy)|temporary failure in name resolution|"
    r"(?:failed to connect|failed connect|unable to connect) to|network is unreachable|"
    r"connection (?:timed out|reset|refused)|operation (?:timed out|too slow)|"
    r"TLS connection was non-properly terminated|unexpected disconnect|early EOF|"
    r"RPC failed; (?:HTTP [0-9]+ )?curl (?:18|28|52|55|56|92)\b|"
    r"the requested URL returned error: (?:408|429|500|502|503|504)\b)",
    re.IGNORECASE | re.MULTILINE,
)
T = TypeVar("T")


def _git(arguments: tuple[str, ...], cwd: Path | None, timeout: float) -> str:
    command = ["git", *arguments]
    with subprocess.Popen(
        command,
        cwd=cwd,
        env={
            **os.environ,
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_HTTP_LOW_SPEED_LIMIT": "1",
            "GIT_HTTP_LOW_SPEED_TIME": "60",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # Stop remote helpers and index-pack before retrying or cleaning up.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, command, stdout, stderr.decode(errors="replace")
            )
    return stdout.decode().strip()


def _retry(operation: str, run: Callable[[], T]) -> T:
    total = 1 + len(RETRY_DELAYS)
    attempt = 1
    while True:
        try:
            return run()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if isinstance(error, subprocess.TimeoutExpired):
                detail = f"attempt timed out after {error.timeout:g}s"
                retryable = True
            else:
                detail = (error.stderr or "").strip()
                if not detail:
                    detail = f"exit status {error.returncode}"
                retryable = TRANSIENT_FAILURE.search(detail) is not None
            message = f"git {operation} attempt {attempt}/{total} failed"
            if not retryable or attempt == total:
                print(f"{message}: {detail}", file=sys.stderr, flush=True)
                raise
            delay = RETRY_DELAYS[attempt - 1]
            print(
                f"{message}; retrying in {delay}s: {detail}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(delay)
        attempt += 1


def remote_git(
    operation: str, *arguments: str, cwd: Path | None = None, timeout: float = 120
) -> str:
    """Run a remote ref lookup or fetch, keeping diagnostics off stdout."""
    return _retry(operation, lambda: _git((operation, *arguments), cwd, timeout))


def clone_repository(
    directory: Path, url: str, *options: str, timeout: float = 900
) -> None:
    """Clone into a fresh sibling staging directory on each attempt."""
    if os.path.lexists(directory):
        raise FileExistsError(f"clone destination already exists: {directory}")

    def clone() -> None:
        with tempfile.TemporaryDirectory(
            prefix=".git-clone-", dir=directory.parent
        ) as temp:
            staging = Path(temp) / directory.name
            _git(("clone", *options, "--", url, str(staging)), None, timeout)
            staging.rename(directory)

    print(f"Cloning into '{directory}'...", file=sys.stderr, flush=True)
    _retry("clone", clone)
