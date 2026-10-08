"""Producer identities and compatible cache/upload/cleanup paths."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

try:
    from . import package_layout as layout
except ImportError:
    import package_layout as layout


class PackageLayoutTests(unittest.TestCase):
    """One layout contract covers builders, restore slots and artifact validation."""

    def test_source_names_round_trip_to_producer_architectures(self):
        """Names may contain dashes/underscores/plus signs without confusing suffixes."""
        for name in ("linux-kernel", "blackbox_exporter", "source+1.0", "name-amd64"):
            for arch in ("amd64", "arm64"):
                with self.subTest(name=name, arch=arch):
                    self.assertEqual(
                        layout.artifact_arch(layout.artifact_name(name, arch)), arch
                    )

    def test_unsafe_names_and_architectures_are_rejected(self):
        """Artifact strings cannot escape the download directory or name a third arch."""
        for name in (
            "deb-../escape-amd64",
            "deb--amd64",
            "deb-source-riscv64",
            "deb-source-arm64-extra",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                layout.artifact_arch(name)
        with self.assertRaises(ValueError):
            layout.artifact_name("../escape", "amd64")
        with self.assertRaises(ValueError):
            layout.artifact_name("source", "all")

    def test_cache_contract_keeps_legacy_metadata_scope_and_order(self):
        """Recipe metadata still uses the shared wildcard, and binaries stay first."""
        for group, directory, metadata in (
            (
                "build",
                "vyos-build/scripts/package-build/frr",
                "vyos-build/scripts/package-build/**",
            ),
            ("build-extra", "packages", "packages"),
        ):
            paths = layout.package_paths(group, "frr")
            expected = (
                f"{directory}/*.deb",
                f"{metadata}/*.buildinfo",
                f"{metadata}/*.changes",
                f"{metadata}/*.dsc",
                f"{metadata}/*debian.tar.*",
                f"{metadata}/*orig.tar.*",
            )
            self.assertEqual(paths.cache, expected)
            self.assertIn(
                "cache<<PATHS\n" + "\n".join(expected) + "\nPATHS\n",
                paths.output_text(),
            )

    def test_cleanup_removes_outputs_but_keeps_source_directories_and_files(self):
        """Batched restores cannot leave binaries behind for the following source."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "packages/source"
            source.mkdir(parents=True)
            keep = source / "debian.rules"
            keep.write_text("keep")
            outputs = [root / "packages/sample.deb", root / "packages/sample.buildinfo"]
            for path in outputs:
                path.write_bytes(b"restored")
            layout.cleanup_outputs(layout.package_paths("build-extra", "source"), root)
            self.assertTrue(keep.exists())
            self.assertTrue(source.is_dir())
            self.assertFalse(any(path.exists() for path in outputs))

    def test_cleanup_never_follows_an_output_symlink(self):
        """A matching output link is unlinked; its external target is not touched."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            (workspace / "packages").mkdir(parents=True)
            outside = root / "outside.deb"
            outside.write_text("keep")
            link = workspace / "packages/output.deb"
            link.symlink_to(outside)
            layout.cleanup_outputs(
                layout.package_paths("build-extra", "source"), workspace
            )
            self.assertFalse(link.is_symlink())
            self.assertEqual(outside.read_text(), "keep")

    def test_cleanup_rejects_symlinked_parent_before_deleting_anything(self):
        """A recipe metadata glob must not traverse a source directory outside the job."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            recipes = workspace / "vyos-build/scripts/package-build"
            (recipes / "frr").mkdir(parents=True)
            local = recipes / "frr/output.deb"
            local.write_text("keep")
            outside = root / "outside"
            outside.mkdir()
            external = outside / "output.buildinfo"
            external.write_text("keep")
            (recipes / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                layout.cleanup_outputs(layout.package_paths("build", "frr"), workspace)
            self.assertTrue(local.exists())
            self.assertTrue(external.exists())

    def test_action_cli_appends_only_to_the_requested_output_file(self):
        """The action and standalone tool share exactly the same validated values."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                self.assertEqual(
                    layout.main(["outputs", "--group", "build", "--package", "frr"]), 0
                )
                before = output.read_bytes()
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        layout.main(
                            ["outputs", "--group", "build", "--package", "../bad"]
                        ),
                        1,
                    )
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(
                output.read_text(), layout.package_paths("build", "frr").output_text()
            )


if __name__ == "__main__":
    unittest.main()
