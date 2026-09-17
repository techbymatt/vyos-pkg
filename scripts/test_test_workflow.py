"""Check the test workflow's static verification wiring."""

import unittest
from pathlib import Path

try:
    from .cache_namespace import job_text
except ImportError:
    from cache_namespace import job_text

WORKFLOW = Path(__file__).parent.parent / ".github/workflows/test.yaml"


class TestWorkflowTests(unittest.TestCase):
    def test_verification_uses_static_checks_on_unprivileged_runners(self) -> None:
        verify = job_text(WORKFLOW.read_text(), "verify")
        self.assertNotIn("    container:", verify)
        self.assertIn("    runs-on: ubuntu-24.04\n", verify)
        self.assertIn("          sparse-checkout: scripts\n", verify)
        self.assertIn("          pattern: deb-*-${{ matrix.arch }}\n", verify)
        self.assertIn(
            'python3 scripts/validate_packages.py --arch "${{ matrix.arch }}" '
            '"${packages[@]}"',
            verify,
        )
        self.assertNotIn("apt-ftparchive", verify)
        self.assertNotIn("--reinstall", verify)

    def test_lintian_is_advisory_with_separate_reports(self) -> None:
        verify = job_text(WORKFLOW.read_text(), "verify")
        self.assertIn("> lintian-report.txt 2>&1 || status=$?", verify)
        self.assertIn("          name: lintian-report-${{ matrix.arch }}\n", verify)

    def test_publication_requires_successful_verification(self) -> None:
        publish = job_text(WORKFLOW.read_text(), "publish")
        self.assertIn("needs.verify.result == 'success'", publish)
        self.assertIn("!contains(needs.*.result, 'failure')", publish)
        self.assertIn("      - verify\n", publish)


if __name__ == "__main__":
    unittest.main()
