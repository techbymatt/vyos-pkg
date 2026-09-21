"""Whole-file namespace scope and checkout integration tests."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from . import cache_namespace as cn
except ImportError:
    import cache_namespace as cn

INPUTS = ("image@sha256:digest", "patch-tree", "shared", "data-tree")
BUILD_INPUTS = (
    ".github/workflows/build-recipe.yaml",
    ".github/workflows/build-standalone.yaml",
    ".github/actions/package-paths/action.yaml",
    ".github/actions/package-artifacts/action.yaml",
    ".github/actions/restore-package/action.yaml",
    "scripts/package_catalog.json",
    "scripts/package_catalog.py",
    "scripts/package_build_policy.py",
    "scripts/prepare_package_build.py",
    "scripts/plan_builds.py",
)
EXCLUDED_INPUTS = (
    ".github/workflows/publish.yaml",
    ".github/workflows/test.yaml",
    ".github/workflows/verify-packages.yaml",
    "scripts/validate_packages.py",
    "scripts/report_lintian.py",
    "scripts/build_repo.sh",
    "scripts/publish_manifest.py",
    "README.md",
    "docs/build.md",
)


class NamespaceTests(unittest.TestCase):
    """Whole-file hashing scope and CLI behavior for build inputs."""

    def setUp(self) -> None:
        """Creates a workflow tree containing every build input file."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workflow = Path(temporary.name)
        for relative in BUILD_INPUTS:
            target = self.workflow / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"whole file fixture\n")

    def namespace(self) -> str:
        """Computes the namespace over the fixture workflow."""
        return cn.namespace(*INPUTS, self.workflow)

    def cli(self, root: Path) -> subprocess.CompletedProcess:
        """Runs the cache_namespace CLI against a workflow root."""
        return subprocess.run(
            [
                sys.executable,
                str(Path(cn.__file__)),
                "--build-image",
                INPUTS[0],
                "--patch-tree",
                INPUTS[1],
                "--shared-build-inputs",
                INPUTS[2],
                "--data-tree",
                INPUTS[3],
                "--workflow",
                str(root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_explicit_build_input_contract(self) -> None:
        """The module's BUILD_INPUTS matches the test's declared inputs."""
        self.assertEqual(tuple(map(str, cn.BUILD_INPUTS)), BUILD_INPUTS)

    def test_each_whole_input_invalidates_caches(self) -> None:
        """Prepending or appending bytes to any input changes the hash."""
        before = self.namespace()
        for relative in BUILD_INPUTS:
            with self.subTest(path=relative):
                target = self.workflow / relative
                original = target.read_bytes()
                for content in (b"prefix\n" + original, original + b"suffix\n"):
                    target.write_bytes(content)
                    self.assertNotEqual(self.namespace(), before)
                target.write_bytes(original)
        self.assertEqual(self.namespace(), before)

    def test_each_missing_input_is_rejected(self) -> None:
        """Deleting any build input fails both the API and the CLI."""
        for relative in BUILD_INPUTS:
            with self.subTest(path=relative):
                target = self.workflow / relative
                original = target.read_bytes()
                target.unlink()
                with self.assertRaises(FileNotFoundError):
                    self.namespace()
                result = self.cli(self.workflow)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(relative, result.stderr)
                target.write_bytes(original)

    def test_orchestration_verification_publication_docs_are_excluded(self) -> None:
        """Editing excluded orchestration, docs, and verify files is ignored."""
        before = self.namespace()
        for relative in EXCLUDED_INPUTS:
            with self.subTest(path=relative):
                target = self.workflow / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original\n", encoding="utf-8")
                self.assertEqual(self.namespace(), before)
                target.write_text("edited\n", encoding="utf-8")
                self.assertEqual(self.namespace(), before)

    def test_each_external_input_changes_the_namespace(self) -> None:
        """Changing any explicit argument changes the namespace."""
        for index in range(len(INPUTS)):
            with self.subTest(index=index):
                values = list(INPUTS)
                values[index] += "-changed"
                self.assertNotEqual(
                    cn.namespace(*values, self.workflow), self.namespace()
                )

    def test_file_boundaries_are_preserved(self) -> None:
        """Moving bytes between files changes the hash."""
        first, second = (self.workflow / path for path in BUILD_INPUTS[:2])
        first.write_bytes(b"ab")
        second.write_bytes(b"c")
        before = self.namespace()
        first.write_bytes(b"a")
        second.write_bytes(b"bc")
        self.assertNotEqual(self.namespace(), before)

    def test_cli_prints_the_namespace(self) -> None:
        """The CLI prints exactly the namespace hash."""
        result = self.cli(self.workflow)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.namespace() + "\n")

    def test_real_checkout_hashing(self) -> None:
        """The CLI hashes the real checkout like the direct call."""
        root = Path(cn.__file__).resolve().parent.parent
        result = self.cli(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\A[0-9a-f]{64}\n\Z")
        self.assertEqual(result.stdout.strip(), cn.namespace(*INPUTS, root))


class JobTextCompatibilityTests(unittest.TestCase):
    """Text-based job block extraction from workflow files."""

    def test_job_boundaries_and_last_job(self) -> None:
        """job_text slices each job block, including the last one."""
        workflow = "jobs:\n  build:\n    steps: []\n  verify:\n    steps: []\n"
        self.assertEqual(cn.job_text(workflow, "build"), "  build:\n    steps: []\n")
        self.assertEqual(cn.job_text(workflow, "verify"), "  verify:\n    steps: []\n")

    def test_missing_job_is_rejected(self) -> None:
        """job_text raises ValueError for unknown job names."""
        with self.assertRaisesRegex(ValueError, "job not found: publish"):
            cn.job_text("jobs:\n  build:\n    steps: []\n", "publish")


if __name__ == "__main__":
    unittest.main()
