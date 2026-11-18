"""Exercise the kernel signing-key retry policy without a keyring or network."""

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import call, patch

try:
    from . import kernel_keys, network_retry
except ImportError:
    import kernel_keys
    import network_retry

ADDRESSES = ("torvalds@kernel.org", "gregkh@kernel.org")
COMMAND = ["gpg2", "--batch", "--no-tty", "--locate-keys", *ADDRESSES]
WKD_TIMEOUT = (
    "gpg: error retrieving 'gregkh@kernel.org' via WKD: Connection timed out\n"
    "gpg: error reading key: Connection timed out\n"
    "gpg: error retrieving 'torvalds@kernel.org' via WKD: Connection timed out\n"
    "gpg: error reading key: Connection timed out\n"
)


class KernelKeyTests(unittest.TestCase):
    def setUp(self):
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.sleep = self.enterContext(patch.object(network_retry.time, "sleep"))

    def test_wkd_timeout_and_hung_lookup_retry_the_same_keys(self):
        with patch.object(
            kernel_keys,
            "command_output",
            side_effect=[
                subprocess.CalledProcessError(2, COMMAND, stderr=WKD_TIMEOUT),
                subprocess.TimeoutExpired(COMMAND, 90),
                "key listing",
            ],
        ) as run:
            kernel_keys.locate_kernel_keys(*ADDRESSES)
        self.assertEqual(
            run.call_args_list, [call(COMMAND, env={"LC_ALL": "C"}, timeout=90)] * 3
        )
        self.assertEqual(self.sleep.call_args_list, [call(5), call(15)])
        self.assertIn("via WKD: Connection timed out", self.stderr.getvalue())
        self.assertIn("attempt timed out after 90s", self.stderr.getvalue())

    def test_exhausted_lookup_propagates_the_final_gpg_error(self):
        error = subprocess.CalledProcessError(2, COMMAND, stderr=WKD_TIMEOUT)
        with (
            patch.object(kernel_keys, "command_output", side_effect=error) as run,
            self.assertRaises(subprocess.CalledProcessError) as result,
        ):
            kernel_keys.locate_kernel_keys(*ADDRESSES)
        self.assertIs(result.exception, error)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn("attempt 3/3 failed", self.stderr.getvalue())

    def test_missing_key_or_bad_wkd_certificate_is_not_retried(self):
        for reason in ("No data", "Bad CA certificate"):
            detail = f"gpg: error retrieving 'gregkh@kernel.org' via WKD: {reason}"
            error = subprocess.CalledProcessError(2, COMMAND, stderr=detail)
            with (
                self.subTest(reason=reason),
                patch.object(kernel_keys, "command_output", side_effect=error) as run,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                kernel_keys.locate_kernel_keys(*ADDRESSES)
            run.assert_called_once()
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
