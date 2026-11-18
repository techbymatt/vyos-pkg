"""Transport retries require no network access or real backoff sleeps in tests."""

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

try:
    from . import git_network as network
    from . import network_retry
except ImportError:
    import git_network as network
    import network_retry


URL = "https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git"
CONNECT_TIMEOUT = (
    f"fatal: unable to access '{URL}/': Failed to connect to git.kernel.org port 443 "
    "after 134539 ms: Couldn't connect to server"
)


def failure(stderr):
    return subprocess.CalledProcessError(128, ["git"], stderr=stderr)


class GitNetworkTests(unittest.TestCase):
    def setUp(self):
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.sleep = self.enterContext(patch.object(network_retry.time, "sleep"))

    def test_transient_error_and_wall_timeout_retry_the_exact_request(self):
        request = ("fetch", "origin", "a" * 40)
        with (
            redirect_stdout(io.StringIO()) as stdout,
            patch.object(
                network,
                "_git",
                side_effect=[
                    failure(CONNECT_TIMEOUT),
                    subprocess.TimeoutExpired(["git", *request], 42),
                    "success",
                ],
            ) as git,
        ):
            self.assertEqual(
                network.remote_git(*request, cwd=Path("repo"), timeout=42), "success"
            )
        self.assertEqual(git.call_args_list, [call(request, Path("repo"), 42)] * 3)
        self.assertEqual(self.sleep.call_args_list, [call(5), call(15)])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(CONNECT_TIMEOUT, self.stderr.getvalue())
        self.assertIn("attempt timed out after 42s", self.stderr.getvalue())

    def test_continuing_outage_exhausts_retries_and_reports_the_git_error(self):
        error = failure("fatal: The requested URL returned error: 503")
        with (
            patch.object(network, "_git", side_effect=error) as git,
            self.assertRaises(subprocess.CalledProcessError) as result,
        ):
            network.remote_git("ls-remote", "--", URL)
        self.assertIs(result.exception, error)
        self.assertEqual(git.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn("attempt 3/3 failed: " + error.stderr, self.stderr.getvalue())

    def test_permanent_failures_do_not_retry(self):
        for detail in (
            "fatal: Authentication failed",
            "fatal: remote error: upload-pack: not our ref",
            "fatal: SSL certificate problem: unable to get local issuer certificate",
            "fatal: The requested URL returned error: 404",
        ):
            with (
                self.subTest(detail=detail),
                patch.object(network, "_git", side_effect=failure(detail)) as git,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                network.remote_git("ls-remote", "--", URL)
            git.assert_called_once()
        self.sleep.assert_not_called()

    def test_clone_retries_in_fresh_staging_and_only_exposes_a_complete_clone(self):
        self.check_clone_staging(complete=True)

    def test_failed_clone_leaves_no_checkout_or_staging(self):
        self.check_clone_staging(complete=False)

    def check_clone_staging(self, *, complete):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "firmware"
            attempts = []

            def git(arguments, cwd, timeout):
                self.assertEqual(
                    arguments[:-1], ("clone", "--branch", "tag", "--", URL)
                )
                self.assertIsNone(cwd)
                self.assertEqual(timeout, 900)
                staging = Path(arguments[-1])
                self.assertFalse(directory.exists())
                self.assertTrue(all(not path.exists() for path in attempts))
                attempts.append(staging)
                staging.mkdir()
                if not complete or len(attempts) == 1:
                    (staging / "partial").touch()
                    raise subprocess.TimeoutExpired(["git"], timeout)
                (staging / "complete").touch()
                return ""

            with patch.object(network, "_git", side_effect=git):
                if complete:
                    network.clone_repository(directory, URL, "--branch", "tag")
                    self.assertTrue((directory / "complete").is_file())
                    self.assertFalse((directory / "partial").exists())
                else:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        network.clone_repository(directory, URL, "--branch", "tag")
            self.assertEqual(len(set(attempts)), 2 if complete else 3)
            self.assertEqual(list(root.iterdir()), [directory] if complete else [])

    def test_existing_clone_destination_is_not_touched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "existing"
            directory.mkdir()
            link = root / "dangling"
            link.symlink_to(root / "missing")
            for destination in (directory, link):
                with (
                    self.subTest(destination=destination),
                    patch.object(network, "_git") as git,
                    self.assertRaises(FileExistsError),
                ):
                    network.clone_repository(destination, URL)
                git.assert_not_called()
            self.assertTrue(directory.is_dir())
            self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
