"""Bounded backoff for explicitly selected remote operations."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from typing import TypeVar

RETRY_DELAYS = (5, 15)
T = TypeVar("T")


def retry_network(
    operation: str, run: Callable[[], T], is_transient: Callable[[str], bool]
) -> T:
    """Retry timeouts and caller-selected failures; preserve the final exception."""
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
                retryable = is_transient(detail)
            message = f"{operation} attempt {attempt}/{total} failed"
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
