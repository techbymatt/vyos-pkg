"""Retry transient Git transport failures without changing source refs or Git config."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

try:
    from .network_retry import retry_network
except ImportError:
    from network_retry import retry_network

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


def _is_transient(detail: str) -> bool:
    return TRANSIENT_FAILURE.search(detail) is not None


def remote_git(
    operation: str, *arguments: str, cwd: Path | None = None, timeout: float = 120
) -> str:
    """Run a remote ref lookup or fetch, keeping diagnostics off stdout."""
    return retry_network(
        f"git {operation}",
        lambda: _git((operation, *arguments), cwd, timeout),
        _is_transient,
    )


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
    retry_network("git clone", clone, _is_transient)
