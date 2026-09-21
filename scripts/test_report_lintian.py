"""Exercise advisory scans with real subprocesses and short deadlines."""

import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
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

    def scan(self, script, **kwargs):
        """Run report_lintian with a stub command."""
        with redirect_stdout(io.StringIO()) as output:
            summary = rl.report_lintian(
                self.packages,
                self.report,
                command=(sys.executable, "-c", script),
                kill_grace=0.02,
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
        summary, _, report = self.scan("import time; time.sleep(30)", total_timeout=0.2)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("timed_out=1", summary)
        self.assertIn("unscanned=2", summary)
        self.assertIn(f"UNSCANNED {self.packages[2]}", report)

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
