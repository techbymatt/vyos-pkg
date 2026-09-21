"""Deterministic tar fixtures with only the external dpkg tools mocked."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

try:
    from . import validate_packages as vp
except ImportError:
    import validate_packages as vp


def tar_bytes(*entries: tuple[str, bytes | tuple[bytes, str]]) -> bytes:
    """Build an in-memory tar archive from (name, content) entries."""
    output = io.BytesIO()
    with vp.tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in entries:
            member = vp.tarfile.TarInfo(name)
            if isinstance(content, tuple):
                member.type, member.linkname = content
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


class ValidatePackagesTests(unittest.TestCase):
    """Covers package validation, archive safety, checksums, and artifact grouping."""
    def setUp(self) -> None:
        """Create a sample package and mock the external dpkg tools."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "sample.deb"
        self.package.write_bytes(b"fixed package bytes")
        self.metadata = dict(
            zip(
                vp.FIELDS,
                (
                    "sample",
                    "1.0-1",
                    "amd64",
                    "Tester <test@example.org>",
                    "Sample\n Long description",
                ),
            )
        )
        self.payload = tar_bytes(("./usr/share/sample", b"hello"))
        self.checksum = hashlib.md5(b"hello", usedforsecurity=False).hexdigest()
        self.control = tar_bytes(
            ("md5sums", f"{self.checksum}  usr/share/sample\n".encode())
        )
        self.failure = None
        self.tool = patch.object(
            vp.subprocess, "run", side_effect=self.external_tool
        ).start()
        self.addCleanup(patch.stopall)

    def external_tool(self, command, *, stdout, stderr, check):
        """Fake dpkg/dpkg-deb serving fixture archives or a scripted failure."""
        if command[1] == self.failure:
            return subprocess.CompletedProcess(command, 2, b"", b"fixture failure")
        if command[1] == "--field":
            return subprocess.CompletedProcess(
                command, 0, self.metadata[command[3]].encode(), b""
            )
        if command[1] == "--validate-version":
            return subprocess.CompletedProcess(command, 0, b"", b"")
        data = {"--ctrl-tarfile": self.control, "--fsys-tarfile": self.payload}[
            command[1]
        ]
        stdout.write(data)
        return subprocess.CompletedProcess(command, 0, None, b"")

    def validate(self) -> None:
        """Validate the sample package for amd64."""
        vp.validate_packages([self.package], "amd64")

    def test_valid_package_checks_version_with_dpkg(self) -> None:
        """Validation shells out to dpkg --validate-version with the control version."""
        self.validate()
        self.assertIn(
            unittest.mock.call(
                ["dpkg", "--validate-version", "1.0-1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ),
            self.tool.call_args_list,
        )

    def test_all_architecture_is_allowed(self) -> None:
        """Architecture: all packages pass an amd64 validation request."""
        self.metadata["Architecture"] = "all"
        self.validate()

    def test_arm64_is_allowed_when_requested(self) -> None:
        """arm64 packages validate when arm64 is the requested architecture."""
        self.metadata["Architecture"] = "arm64"
        vp.validate_packages([self.package], "arm64")

    def test_wrong_architecture(self) -> None:
        """Packages whose Architecture differs from the request are rejected."""
        self.metadata["Architecture"] = "arm64"
        with self.assertRaisesRegex(ValueError, "unexpected Architecture"):
            self.validate()

    def test_each_required_field(self) -> None:
        """Blanking any required control field raises a metadata error."""
        for field in vp.FIELDS:
            with (
                self.subTest(field=field),
                patch.dict(self.metadata, {field: " "}),
                self.assertRaisesRegex(ValueError, f"required metadata: {field}"),
            ):
                self.validate()

    def test_invalid_package_name(self) -> None:
        """Package names violating Debian naming rules are rejected."""
        self.metadata["Package"] = "bad_name"
        with self.assertRaisesRegex(ValueError, "invalid Package"):
            self.validate()

    def test_invalid_version(self) -> None:
        """A failing dpkg --validate-version call surfaces as a ValueError."""
        self.failure = "--validate-version"
        with self.assertRaisesRegex(ValueError, "validate-version failed"):
            self.validate()

    def test_corrupt_control_archive(self) -> None:
        """Non-tar control data raises ValueError."""
        self.control = b"not tar"
        with self.assertRaises(ValueError):
            self.validate()

    def test_truncated_payload_file(self) -> None:
        """A member cut off before its declared size raises an end-of-data error."""
        self.payload = tar_bytes(("large", b"x" * 4096))[:1024]
        with self.assertRaisesRegex(ValueError, "unexpected end"):
            self.validate()

    def test_missing_archive_end_markers(self) -> None:
        """Payload lacking tar end blocks raises ValueError."""
        self.payload = self.payload[:1024]
        with self.assertRaisesRegex(ValueError, "end markers"):
            self.validate()

    def test_trailing_archive_garbage(self) -> None:
        """Extra bytes after the end markers raise a nonzero-data error."""
        self.payload += b"garbage"
        with self.assertRaisesRegex(ValueError, "nonzero data"):
            self.validate()

    def test_decompressor_failure_is_not_ignored(self) -> None:
        """A failing --fsys-tarfile subprocess raises instead of yielding empty data."""
        self.failure = "--fsys-tarfile"
        with self.assertRaisesRegex(ValueError, "fsys-tarfile failed"):
            self.validate()

    def test_unsafe_payload_paths(self) -> None:
        """Payload members with parent-relative or absolute names are rejected."""
        for name in ("../outside", "/absolute", "usr/../../outside"):
            with self.subTest(name=name):
                self.payload = tar_bytes((name, b"hello"))
                with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                    self.validate()

    def test_unsafe_control_path(self) -> None:
        """Control members escaping the archive root are rejected."""
        self.control = tar_bytes(("../postinst", b"exit 1"))
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            self.validate()

    def test_absolute_symlink_and_scripts_are_inert(self) -> None:
        """Maintainer scripts and symlinks are inspected, never executed or extracted."""
        self.control = tar_bytes(("postinst", b"#!/bin/sh\nexit 99\n"))
        self.payload = tar_bytes(("link", (vp.tarfile.SYMTYPE, "/etc/passwd")))
        self.validate()
        self.assertEqual(
            [call.args[0][0] for call in self.tool.call_args_list],
            ["dpkg-deb"] * 5 + ["dpkg", "dpkg-deb", "dpkg-deb"],
        )
        self.assertEqual(list(self.root.iterdir()), [self.package])

    def test_member_under_symlink_is_rejected(self) -> None:
        """Members whose parent is a symlink raise a non-directory-parent error."""
        self.payload = tar_bytes(
            ("usr", (vp.tarfile.SYMTYPE, "/tmp")), ("usr/file", b"hello")
        )
        with self.assertRaisesRegex(ValueError, "non-directory archive parent"):
            self.validate()

    def test_hardlink_checksum(self) -> None:
        """Hardlinks validate against the checksum of their resolved target."""
        self.payload = tar_bytes(
            ("original", b"hello"),
            ("usr/share/sample", (vp.tarfile.LNKTYPE, "original")),
        )
        self.validate()

    def test_unsafe_hardlink_target(self) -> None:
        """Hardlinks pointing outside the archive are rejected."""
        self.payload = tar_bytes(("file", (vp.tarfile.LNKTYPE, "../outside")))
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            self.validate()

    def test_missing_hardlink_target(self) -> None:
        """Hardlinks with no matching earlier member are rejected."""
        self.payload = tar_bytes(("file", (vp.tarfile.LNKTYPE, "absent")))
        with self.assertRaisesRegex(ValueError, "invalid hardlink target"):
            self.validate()

    def test_duplicate_archive_member(self) -> None:
        """The same path appearing twice in one archive is rejected."""
        self.payload = tar_bytes(("file", b"one"), ("./file", b"two"))
        with self.assertRaisesRegex(ValueError, "duplicate archive path"):
            self.validate()

    def test_optional_checksums(self) -> None:
        """A control archive without md5sums still validates."""
        self.control = tar_bytes(("control", b"metadata"))
        self.validate()

    def test_malformed_checksums(self) -> None:
        """Unparseable md5sums entries raise ValueError."""
        self.control = tar_bytes(("md5sums", b"not a checksum\n"))
        with self.assertRaisesRegex(ValueError, "malformed md5sums"):
            self.validate()

    def test_checksum_mismatch(self) -> None:
        """Payload bytes disagreeing with md5sums raise a mismatch error."""
        self.payload = tar_bytes(("usr/share/sample", b"changed"))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.validate()

    def test_checksum_missing_file(self) -> None:
        """Files listed in md5sums but absent from the payload are rejected."""
        self.payload = tar_bytes(("different", b"hello"))
        with self.assertRaisesRegex(ValueError, "missing regular file"):
            self.validate()

    def test_identical_duplicates(self) -> None:
        """Duplicate paths with byte-identical packages validate together."""
        duplicate = self.root / "duplicate.deb"
        duplicate.write_bytes(self.package.read_bytes())
        vp.validate_packages([self.package, duplicate], "amd64")

    def test_conflicting_duplicates(self) -> None:
        """Duplicate paths with differing bytes raise a SHA256 error."""
        duplicate = self.root / "duplicate.deb"
        duplicate.write_bytes(b"different bytes")
        with self.assertRaisesRegex(ValueError, "differing SHA256"):
            vp.validate_packages([self.package, duplicate], "amd64")

    def test_missing_tool_cli_error(self) -> None:
        """A missing dpkg-deb binary exits 1 with the error and path on stderr."""
        self.tool.side_effect = FileNotFoundError("dpkg-deb not found")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = vp.main(["--arch", "amd64", str(self.package)])
        self.assertEqual(status, 1)
        self.assertIn("dpkg-deb not found", stderr.getvalue())
        self.assertIn(str(self.package), stderr.getvalue())

    def test_cli_success(self) -> None:
        """main returns 0 for a valid package."""
        self.assertEqual(vp.main(["--arch", "amd64", str(self.package)]), 0)

    def test_cli_requires_paths(self) -> None:
        """main exits with usage error 2 when no package paths are given."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            vp.main(["--arch", "amd64"])
        self.assertEqual(error.exception.code, 2)

    def test_missing_package(self) -> None:
        """Absent package paths are rejected as non-.deb files."""
        self.package.unlink()
        with self.assertRaisesRegex(ValueError, "regular .deb file"):
            self.validate()

    def artifact_package(self, name: str) -> Path:
        """Place the sample package under an artifact group directory."""
        path = self.root / name / "nested" / "sample.deb"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.package.read_bytes())
        return path.resolve()

    def test_artifacts_cross_arch_all_identity(self) -> None:
        """Architecture: all must come only from amd64 and match across groups."""
        self.artifact_package("deb-linux-amd64")
        arm = self.artifact_package("deb-linux-arm64")
        self.metadata["Architecture"] = "all"
        with self.assertRaisesRegex(ValueError, "produced by amd64 only"):
            vp.validate_artifacts(self.root)
        arm.write_bytes(b"different all package")
        with self.assertRaisesRegex(ValueError, "produced by amd64 only"):
            vp.validate_artifacts(self.root)

    def test_mixed_artifacts_with_single_all_producer(self) -> None:
        """An Architecture: all package in amd64 plus matching arm64-only output passes."""
        self.artifact_package("deb-linux-amd64")
        arm = self.artifact_package("deb-linux-arm64")

        def tool(command, **kwargs):
            """Report Architecture: all for amd64 and arm64 for the arm64 copy."""
            if command[1] == "--field":
                self.metadata["Architecture"] = (
                    "arm64" if command[2] == str(arm) else "all"
                )
            return self.external_tool(command, **kwargs)

        self.tool.side_effect = tool
        vp.validate_artifacts(self.root)

    def test_artifacts_match_each_package_architecture(self) -> None:
        """Each package's Architecture must match its artifact group's architecture."""
        amd = self.artifact_package("deb-linux-amd64")
        arm = self.artifact_package("deb-linux-arm64")
        extra = self.artifact_package("deb-other-amd64")
        architectures = {str(amd): "amd64", str(arm): "arm64", str(extra): "amd64"}

        def tool(command, **kwargs):
            """Report each package's architecture from the fixture map."""
            if command[1] == "--field":
                self.metadata["Architecture"] = architectures[command[2]]
            return self.external_tool(command, **kwargs)

        self.tool.side_effect = tool
        vp.validate_artifacts(self.root)
        for path, wrong in ((amd, "arm64"), (arm, "amd64"), (extra, "arm64")):
            with (
                self.subTest(path=path),
                patch.dict(architectures, {str(path): wrong}),
                self.assertRaisesRegex(ValueError, "unexpected Architecture"),
            ):
                vp.validate_artifacts(self.root)

    def test_artifacts_empty_or_missing_groups(self) -> None:
        """Missing or empty expected architecture groups raise ValueError."""
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                other = "arm64" if arch == "amd64" else "amd64"
                (root / f"deb-linux-{other}").mkdir()
                (root / f"deb-linux-{other}" / "sample.deb").write_bytes(b"deb")
                for empty in (False, True):
                    if empty:
                        (root / f"deb-linux-{arch}").mkdir()
                    with self.assertRaisesRegex(
                        ValueError, f"(group: {arch}|empty artifact directory)"
                    ):
                        vp.validate_artifacts(root)
        with self.assertRaisesRegex(ValueError, "empty or missing"):
            vp.validate_artifacts(self.root)

    def test_artifacts_reject_unknown_directory_names(self) -> None:
        """Artifact directories outside the deb-<source>-<arch> pattern are rejected."""
        self.artifact_package("deb-linux-amd64")
        self.artifact_package("deb-linux-arm64")
        for name in (
            "linux-amd64",
            "deb-linux-riscv64",
            "deb-linux",
            "deb-linux-arm64-extra",
        ):
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                with self.assertRaisesRegex(ValueError, "unknown artifact directory"):
                    vp.validate_artifacts(self.root)
                directory.rmdir()

    def test_artifacts_missing_root_cli_error(self) -> None:
        """A missing --artifacts root exits 1 through main's error handling."""
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                vp.main(
                    [
                        "--artifacts",
                        str(self.root / "absent"),
                        "--expected-arches",
                        '["amd64","arm64"]',
                    ]
                ),
                1,
            )

    def test_single_expected_architecture(self) -> None:
        """Expected arches [amd64] accepts an Architecture: all package from amd64."""
        self.artifact_package("deb-sample-amd64")
        self.metadata["Architecture"] = "all"
        self.assertEqual(
            vp.main(["--artifacts", str(self.root), "--expected-arches", '["amd64"]']),
            0,
        )

    def test_single_arm64_expected_architecture(self) -> None:
        """Expected arches [arm64] accepts arm64 packages."""
        self.artifact_package("deb-sample-arm64")
        self.metadata["Architecture"] = "arm64"
        vp.validate_artifacts(self.root, ["arm64"])

    def test_unexpected_architecture_group(self) -> None:
        """Artifact groups outside the expected architecture set raise ValueError."""
        self.artifact_package("deb-sample-arm64")
        with self.assertRaisesRegex(ValueError, "unexpected artifact architecture"):
            vp.validate_artifacts(self.root, ["amd64"])

    def test_invalid_expected_architecture_sets(self) -> None:
        """Malformed expected-arches inputs (empty, dupes, non-list) raise ValueError."""
        for arches in ([], ["all"], ["amd64", "amd64"], "amd64", {}, [None]):
            with (
                self.subTest(arches=arches),
                self.assertRaisesRegex(ValueError, "expected architectures"),
            ):
                vp.validate_artifacts(self.root, arches)

    def test_artifact_duplicates_checked_across_sources(self) -> None:
        """Identical duplicates across artifact sources pass; differing ones fail."""
        self.artifact_package("deb-recipe-amd64")
        duplicate = self.artifact_package("deb-standalone-amd64")
        vp.validate_artifacts(self.root, ["amd64"])
        duplicate.write_bytes(b"different package bytes")
        with self.assertRaisesRegex(ValueError, "differing SHA256"):
            vp.validate_artifacts(self.root, ["amd64"])

    def test_empty_artifact_rejected_even_with_other_outputs(self) -> None:
        """An empty group fails even when other expected groups have packages."""
        self.artifact_package("deb-sample-amd64")
        (self.root / "deb-empty-amd64").mkdir()
        with self.assertRaisesRegex(ValueError, "empty artifact directory"):
            vp.validate_artifacts(self.root, ["amd64"])

    def test_cli_rejects_mixed_modes(self) -> None:
        """Combining --artifacts with package paths, --arch, or neither exits 2."""
        for arguments in (
            ["--artifacts", str(self.root), "--arch", "amd64"],
            ["--artifacts", str(self.root), str(self.package)],
            ["--artifacts", str(self.root)],
            ["--arch", "amd64", str(self.package), "--expected-arches", '["amd64"]'],
            [],
        ):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    vp.main(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
