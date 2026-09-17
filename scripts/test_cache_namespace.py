"""Namespace scope tests with fixture workflow files."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from . import cache_namespace as cn
except ImportError:
    import cache_namespace as cn

WORKFLOW = """\
concurrency:
  group: publish
env:
  BUILD_IMAGE: image
jobs:
  cache-check:
    name: Cache check
    steps:
      - run: plan
  build:
    name: Build
    steps:
      - run: docker run image
  build-extra:
    name: Build extra
    steps:
      - run: docker build
  restore-cached:
    name: Restore
    steps:
      - run: restore
  verify:
    name: Verify
    steps:
      - run: validate
"""

COMPOSITE = """\
name: restore-package
runs:
  steps:
    - uses: actions/cache/restore
"""

INPUTS = ("image", "patch-tree", "shared", "data-tree")
DEPENDENCIES = '#!/usr/bin/env bash\necho "gnat gprbuild"\n'


class NamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workflow = Path(temporary.name)
        target = self.workflow / ".github/workflows"
        target.mkdir(parents=True)
        (target / "publish.yaml").write_text(WORKFLOW, encoding="utf-8")
        composite = self.workflow / ".github/actions/restore-package"
        composite.mkdir(parents=True)
        (composite / "action.yaml").write_text(COMPOSITE, encoding="utf-8")
        dependencies = self.workflow / cn.DEPENDENCIES_RELATIVE
        dependencies.parent.mkdir(parents=True)
        dependencies.write_text(DEPENDENCIES, encoding="utf-8")

    def namespace(self) -> str:
        return cn.namespace(*INPUTS, self.workflow)

    def write_workflow(self, content: str) -> None:
        target = self.workflow / ".github/workflows/publish.yaml"
        target.write_text(content, encoding="utf-8")

    def test_extracted_jobs_cover_the_full_recipe(self) -> None:
        self.assertEqual(
            cn.build_surface(self.workflow),
            "  build:\n    name: Build\n    steps:\n      - run: docker run image\n"
            "  build-extra:\n    name: Build extra\n    steps:\n"
            "      - run: docker build\n" + COMPOSITE + DEPENDENCIES,
        )

    def test_last_job_block_extends_to_the_end_of_the_file(self) -> None:
        self.assertEqual(
            cn.job_text(WORKFLOW, "verify"),
            "  verify:\n    name: Verify\n    steps:\n      - run: validate\n",
        )

    def test_missing_job_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "job not found: publish"):
            cn.job_text(WORKFLOW, "publish")

    def test_verification_edits_do_not_change_the_namespace(self) -> None:
        before = self.namespace()
        self.write_workflow(
            WORKFLOW.replace("- run: validate", "- run: validate differently")
        )
        self.assertEqual(self.namespace(), before)

    def test_build_job_edits_change_the_namespace(self) -> None:
        before = self.namespace()
        self.write_workflow(WORKFLOW.replace("docker run image", "docker run other"))
        self.assertNotEqual(self.namespace(), before)

    def test_build_extra_job_edits_change_the_namespace(self) -> None:
        before = self.namespace()
        self.write_workflow(WORKFLOW.replace("docker build", "docker build --pull"))
        self.assertNotEqual(self.namespace(), before)

    def test_restore_package_edits_change_the_namespace(self) -> None:
        before = self.namespace()
        composite = self.workflow / ".github/actions/restore-package/action.yaml"
        composite.write_text(COMPOSITE + "      - run: extra\n", encoding="utf-8")
        self.assertNotEqual(self.namespace(), before)

    def test_publication_edits_do_not_change_the_namespace(self) -> None:
        publish = "  publish:\n    steps:\n      - run: deploy\n"
        self.write_workflow(WORKFLOW + publish)
        before = self.namespace()
        self.write_workflow(WORKFLOW + publish.replace("deploy", "deploy differently"))
        self.assertEqual(self.namespace(), before)

    def test_dependency_edits_change_the_namespace(self) -> None:
        before = self.namespace()
        dependencies = self.workflow / cn.DEPENDENCIES_RELATIVE
        dependencies.write_text(
            DEPENDENCIES.replace("gnat gprbuild", "gnat gprbuild libssl-dev"),
            encoding="utf-8",
        )
        self.assertNotEqual(self.namespace(), before)

    def test_missing_dependency_file_is_rejected(self) -> None:
        (self.workflow / cn.DEPENDENCIES_RELATIVE).unlink()
        with self.assertRaises(OSError):
            self.namespace()

    def test_each_non_recipe_input_changes_the_namespace(self) -> None:
        for index in range(len(INPUTS)):
            with self.subTest(index=index):
                values = list(INPUTS)
                values[index] = "other"
                self.assertNotEqual(
                    cn.namespace(*values, self.workflow), self.namespace()
                )

    def test_missing_workflow_file_is_rejected(self) -> None:
        empty = self.workflow / "empty"
        empty.mkdir()
        with self.assertRaises(OSError):
            cn.namespace(*INPUTS, empty)

    def test_cli_prints_the_namespace(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(cn.__file__)),
                "--build-image",
                "image",
                "--patch-tree",
                "patch-tree",
                "--shared-build-inputs",
                "shared",
                "--data-tree",
                "data-tree",
                "--workflow",
                str(self.workflow),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        digest = hashlib.sha256()
        for value in (*INPUTS, cn.build_surface(self.workflow)):
            digest.update(value.encode("utf-8"))
            digest.update(b"\x00")
        self.assertEqual(result.stdout, digest.hexdigest() + "\n")


if __name__ == "__main__":
    unittest.main()
