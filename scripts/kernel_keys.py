"""Retry the kernel recipe's public signing-key lookup, not verification."""

from __future__ import annotations

import re

try:
    from .network_retry import retry_network
    from .workflow_utils import command_output
except ImportError:
    from network_retry import retry_network
    from workflow_utils import command_output

LOOKUP_TIMEOUT = 90  # Dirmngr can try multiple addresses for each key.
TRANSIENT_WKD = re.compile(
    r"^gpg: error retrieving '[^']+' via WKD: (?:"
    r"(?:connection|operation) timed out|connection reset by peer|connection refused|"
    r"network is unreachable|temporary failure in name resolution|try again)$",
    re.IGNORECASE | re.MULTILINE,
)


def _is_transient(detail: str) -> bool:
    return TRANSIENT_WKD.search(detail) is not None


def locate_kernel_keys(*addresses: str) -> None:
    """Look up the original signer addresses in the same keyring used for verification."""
    command = ["gpg2", "--batch", "--no-tty", "--locate-keys", *addresses]
    retry_network(
        "GPG kernel signing-key lookup (" + ", ".join(addresses) + ")",
        lambda: command_output(command, env={"LC_ALL": "C"}, timeout=LOOKUP_TIMEOUT),
        _is_transient,
    )
