"""Exercise advisory scans with real subprocesses and short deadlines."""

import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    from . import report_lintian as rl
except ImportError:
    import report_lintian as rl


class ReportTests(unittest.TestCase):
    """Subprocess-driven scanning with failures, timeouts, and cleanup."""

    def setUp(self):
        """Create a temporary workspace."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / "report.txt"
        self.packages = [self.root / f"package {index}.deb" for index in range(3)]

    def scan(self, script, jobs=4, **kwargs):
        """Run report_lintian with a stub command."""
        with redirect_stdout(io.StringIO()) as output:
            summary = rl.report_lintian(
                self.packages,
                self.report,
                command=(sys.executable, "-c", script),
                kill_grace=0.02,
                jobs=jobs,
                **kwargs,
            )
        return summary, output.getvalue(), self.report.read_text()

    def test_findings_are_completed_and_output_is_preserved(self):
        """Findings are written to the report and warned on stdout."""
        summary, console, report = self.scan(
            "import sys; print('package finding'); sys.exit(2)"
        )
        self.assertIn("scan complete", summary)
        self.assertIn("completed=3, findings=3", summary)
        self.assertEqual(report.count("package finding"), 3)
        self.assertIn("::warning::", console)
        self.assertIn("START 3/3", console)

    def test_tool_errors_do_not_abort_remaining_packages(self):
        """A failing package does not stop the remaining scans."""
        summary, _, report = self.scan("raise SystemExit(1)")
        self.assertIn("INCOMPLETE", summary)
        self.assertIn("failed=3", summary)
        self.assertIn("START 3/3", report)

    def test_package_timeout_preserves_partial_output_and_continues(self):
        """A timed-out package keeps partial output and scanning continues."""
        summary, _, report = self.scan(
            "import time; print('partial output', flush=True); time.sleep(30)",
            package_timeout=0.2,
        )
        self.assertIn("timed_out=3", summary)
        self.assertEqual(report.count("partial output"), 3)
        self.assertIn("START 3/3", report)

    def test_total_budget_limits_current_package_and_marks_rest_unscanned(self):
        """The total deadline stops the current package and marks the rest unscanned."""
        start = time.monotonic()
        summary, _, report = self.scan(
            "import time; time.sleep(30)", total_timeout=0.2, jobs=1
        )
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("timed_out=1", summary)
        self.assertIn("unscanned=2", summary)
        self.assertIn(f"UNSCANNED {self.packages[2]}", report)

    def test_total_budget_at_width_marks_only_queued_unscanned(self):
        """In-flight packages time out at the deadline; only queued ones are unscanned."""
        summary, _, report = self.scan(
            "import time; time.sleep(30)", total_timeout=0.2, jobs=2
        )
        self.assertIn("timed_out=2", summary)
        self.assertIn("unscanned=1", summary)
        self.assertIn(f"UNSCANNED {self.packages[2]}", report)

    def test_parallel_jobs_scan_concurrently(self):
        """One job per package lets stubs meet at a file barrier."""
        self.packages = [self.root / f"package {index}.deb" for index in range(4)]
        script = (
            "import pathlib, sys, time\n"
            "root = pathlib.Path(sys.argv[1]).parent\n"
            "index = int(pathlib.Path(sys.argv[1]).stem.split()[1])\n"
            "(root / ('ready-%d' % index)).touch()\n"
            "deadline = time.time() + 3\n"
            "found = False\n"
            "while time.time() < deadline:\n"
            "    if all((root / ('ready-%d' % i)).exists() for i in range(4)):\n"
            "        found = True\n"
            "        break\n"
            "    time.sleep(0.01)\n"
            "print('synced' if found else 'timeout', index)\n"
        )
        summary, _, report = self.scan(script, jobs=4, package_timeout=10)
        self.assertIn("completed=4", summary)
        self.assertNotIn("timeout", report)
        self.assertEqual(report.count("synced"), 4)

    def test_report_keeps_input_order_when_completion_flips(self):
        """Out-of-order completion still writes report sections in input order."""
        self.packages = [self.root / f"package {index}.deb" for index in range(3)]
        script = (
            "import pathlib, sys, time\n"
            "index = int(pathlib.Path(sys.argv[1]).stem.split()[1])\n"
            "time.sleep((2 - index) * 0.3)\n"
            "print('finding', index)\n"
        )
        summary, _, report = self.scan(script, jobs=3, package_timeout=10)
        self.assertIn("completed=3", summary)
        self.assertLess(report.index("finding 0"), report.index("finding 2"))
        self.assertLess(report.index("START 1/3"), report.index("START 2/3"))
        self.assertLess(report.index("START 2/3"), report.index("START 3/3"))

    def test_jobs_must_be_positive(self):
        """Non-positive or non-numeric --jobs values are rejected."""
        for value in ("0", "-1", "nope"):
            with (
                self.subTest(value=value),
                self.assertRaises(SystemExit) as raised,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                rl.main(["--artifacts", str(self.root), "--jobs", value])
            self.assertNotEqual(raised.exception.code, 0)

    def test_timeout_kills_descendant_that_ignores_term(self):
        """Timeout kills process-group descendants that ignore SIGTERM."""
        marker = self.root / "child-survived"
        ready = self.root / "child-ready"
        child = (
            "import signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(ready)!r}).touch(); time.sleep(1); "
            f"Path({str(marker)!r}).touch()"
        )
        script = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(30)"
        )
        self.packages = self.packages[:1]
        summary, _, _ = self.scan(script, package_timeout=0.5)
        self.assertTrue(ready.exists(), "descendant must start before timeout")
        self.assertIn("timed_out=1", summary)
        time.sleep(1)
        self.assertFalse(marker.exists(), "descendant survived process-group cleanup")

    def test_missing_lintian_is_reported(self):
        """A missing command is reported as failed."""
        with redirect_stdout(io.StringIO()):
            summary = rl.report_lintian(
                self.packages, self.report, command=(str(self.root / "missing"),)
            )
        self.assertIn("failed=3", summary)
        self.assertIn("No such file", self.report.read_text())

    def test_empty_input_is_incomplete(self):
        """No packages yields an INCOMPLETE summary."""
        self.packages = []
        summary, _, _ = self.scan("raise SystemExit(0)")
        self.assertIn("INCOMPLETE", summary)

    def test_main_exit_code_reflects_scan_completeness(self):
        """main returns 1 for incomplete scans and 0 for complete ones."""
        arguments = [
            "--artifacts",
            str(self.root),
            "--report",
            str(self.report),
        ]
        for incomplete, expected in ((True, 1), (False, 0)):
            with (
                self.subTest(incomplete=incomplete),
                patch.object(rl, "scan_packages", return_value=("summary", incomplete)),
            ):
                self.assertEqual(rl.main(arguments), expected)


if __name__ == "__main__":
    unittest.main()
