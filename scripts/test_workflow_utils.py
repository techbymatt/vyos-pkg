"""Shared command, canonical JSON, output and checked-edit IO contracts."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import workflow_utils as utils
    from .checked_edits import CheckedEdits
except ImportError:
    import workflow_utils as utils
    from checked_edits import CheckedEdits


class WorkflowUtilsTests(unittest.TestCase):
    """IO remains bounded and refactors preserve exact serialization/file modes."""

    def test_canonical_json_matches_the_source_encoding(self):
        """UTF-8, ordering and separators stay compatible with deployed source keys."""
        self.assertEqual(
            utils.canonical_json({"z": "é", "a": [1, 2]}), b'{"a":[1,2],"z":"\xc3\xa9"}'
        )

    def test_commands_are_noninteractive_and_preserve_requested_whitespace(self):
        """Command helpers share noninteractive Git policy and explicit strip behavior."""
        command = [
            sys.executable,
            "-c",
            "import os; print(os.environ['GIT_TERMINAL_PROMPT'] + '  ')",
        ]
        self.assertEqual(utils.command_output(command), "0")
        self.assertEqual(utils.command_output(command, strip=False), "0  \n")

    def test_command_failures_keep_stderr(self):
        """Metadata-service errors retain evidence for diagnostics and legacy proof."""
        with self.assertRaises(subprocess.CalledProcessError) as error:
            utils.command_output(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('HTTP 503', file=sys.stderr); sys.exit(1)",
                ]
            )
        self.assertEqual(error.exception.stderr, "HTTP 503\n")

    def test_commands_have_a_deadline(self):
        """A stuck metadata command cannot consume the entire planning job."""
        with self.assertRaises(subprocess.TimeoutExpired):
            utils.command_output(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1
            )

    def test_output_names_and_values_cannot_inject_assignments(self):
        """Planner stdout contains only safe, single-line assignments."""
        self.assertEqual(
            list(utils.output_lines({"changed": True, "matrix": {"include": []}})),
            ["changed=true", 'matrix={"include":[]}'],
        )
        for name, value in (
            ("bad\nname", "x"),
            ("=bad", "x"),
            ("name", "a\rb"),
            ("name", "a\nb"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                list(utils.output_lines({name: value}))

    def test_atomic_writes_preserve_executable_targets_and_symlinks(self):
        """Updating a shared builder must not replace its recipe symlink."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "build.py"
            target.write_text("old")
            target.chmod(0o755)
            link = root / "recipe.py"
            link.symlink_to("build.py")
            utils.atomic_write(link, b"new")
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()), ["build.py", "recipe.py"]
            )

    def test_failed_atomic_write_keeps_existing_contents(self):
        """A replacement failure does not leave a truncated catalog or manifest."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_bytes(b"original")
            with (
                patch.object(utils.os, "replace", side_effect=OSError("fixture")),
                self.assertRaises(OSError),
            ):
                utils.atomic_write(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_checked_edits_validate_every_anchor_before_writing(self):
        """An upstream drift in a later file leaves earlier adaptations untouched."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.write_text("audited")
            second.write_text("drifted")
            edits = CheckedEdits()
            edits.replace(first, "audited", "adapted")
            with self.assertRaisesRegex(ValueError, "audited command"):
                edits.replace(second, "expected", "adapted")
            self.assertEqual(first.read_text(), "audited")
            self.assertEqual(second.read_text(), "drifted")

    def test_checked_edits_compose_and_require_exact_counts(self):
        """Multi-source commands and sequential replacements share one staged file."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "builder"
            path.write_text("old old import")
            edits = CheckedEdits()
            edits.replace(path, "old", "new", count=2)
            edits.replace(path, "import", "new import")
            self.assertEqual(path.read_text(), "old old import")
            edits.commit()
            self.assertEqual(path.read_text(), "new new new import")


if __name__ == "__main__":
    unittest.main()
