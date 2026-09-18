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
    def setUp(self) -> None:
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
        vp.validate_packages([self.package], "amd64")

    def test_valid_package_checks_version_with_dpkg(self) -> None:
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
        self.metadata["Architecture"] = "all"
        self.validate()

    def test_arm64_is_allowed_when_requested(self) -> None:
        self.metadata["Architecture"] = "arm64"
        vp.validate_packages([self.package], "arm64")

    def test_wrong_architecture(self) -> None:
        self.metadata["Architecture"] = "arm64"
        with self.assertRaisesRegex(ValueError, "unexpected Architecture"):
            self.validate()

    def test_each_required_field(self) -> None:
        for field in vp.FIELDS:
            with (
                self.subTest(field=field),
                patch.dict(self.metadata, {field: " "}),
                self.assertRaisesRegex(ValueError, f"required metadata: {field}"),
            ):
                self.validate()

    def test_invalid_package_name(self) -> None:
        self.metadata["Package"] = "bad_name"
        with self.assertRaisesRegex(ValueError, "invalid Package"):
            self.validate()

    def test_invalid_version(self) -> None:
        self.failure = "--validate-version"
        with self.assertRaisesRegex(ValueError, "validate-version failed"):
            self.validate()

    def test_corrupt_control_archive(self) -> None:
        self.control = b"not tar"
        with self.assertRaises(ValueError):
            self.validate()

    def test_truncated_payload_file(self) -> None:
        self.payload = tar_bytes(("large", b"x" * 4096))[:1024]
        with self.assertRaisesRegex(ValueError, "unexpected end"):
            self.validate()

    def test_missing_archive_end_markers(self) -> None:
        self.payload = self.payload[:1024]
        with self.assertRaisesRegex(ValueError, "end markers"):
            self.validate()

    def test_trailing_archive_garbage(self) -> None:
        self.payload += b"garbage"
        with self.assertRaisesRegex(ValueError, "nonzero data"):
            self.validate()

    def test_decompressor_failure_is_not_ignored(self) -> None:
        self.failure = "--fsys-tarfile"
        with self.assertRaisesRegex(ValueError, "fsys-tarfile failed"):
            self.validate()

    def test_unsafe_payload_paths(self) -> None:
        for name in ("../outside", "/absolute", "usr/../../outside"):
            with self.subTest(name=name):
                self.payload = tar_bytes((name, b"hello"))
                with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                    self.validate()

    def test_unsafe_control_path(self) -> None:
        self.control = tar_bytes(("../postinst", b"exit 1"))
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            self.validate()

    def test_absolute_symlink_and_scripts_are_inert(self) -> None:
        self.control = tar_bytes(("postinst", b"#!/bin/sh\nexit 99\n"))
        self.payload = tar_bytes(("link", (vp.tarfile.SYMTYPE, "/etc/passwd")))
        self.validate()
        self.assertEqual(
            [call.args[0][0] for call in self.tool.call_args_list],
            ["dpkg-deb"] * 5 + ["dpkg", "dpkg-deb", "dpkg-deb"],
        )
        self.assertEqual(list(self.root.iterdir()), [self.package])

    def test_member_under_symlink_is_rejected(self) -> None:
        self.payload = tar_bytes(
            ("usr", (vp.tarfile.SYMTYPE, "/tmp")), ("usr/file", b"hello")
        )
        with self.assertRaisesRegex(ValueError, "non-directory archive parent"):
            self.validate()

    def test_hardlink_checksum(self) -> None:
        self.payload = tar_bytes(
            ("original", b"hello"),
            ("usr/share/sample", (vp.tarfile.LNKTYPE, "original")),
        )
        self.validate()

    def test_unsafe_hardlink_target(self) -> None:
        self.payload = tar_bytes(("file", (vp.tarfile.LNKTYPE, "../outside")))
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            self.validate()

    def test_missing_hardlink_target(self) -> None:
        self.payload = tar_bytes(("file", (vp.tarfile.LNKTYPE, "absent")))
        with self.assertRaisesRegex(ValueError, "invalid hardlink target"):
            self.validate()

    def test_duplicate_archive_member(self) -> None:
        self.payload = tar_bytes(("file", b"one"), ("./file", b"two"))
        with self.assertRaisesRegex(ValueError, "duplicate archive path"):
            self.validate()

    def test_optional_checksums(self) -> None:
        self.control = tar_bytes(("control", b"metadata"))
        self.validate()

    def test_malformed_checksums(self) -> None:
        self.control = tar_bytes(("md5sums", b"not a checksum\n"))
        with self.assertRaisesRegex(ValueError, "malformed md5sums"):
            self.validate()

    def test_checksum_mismatch(self) -> None:
        self.payload = tar_bytes(("usr/share/sample", b"changed"))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.validate()

    def test_checksum_missing_file(self) -> None:
        self.payload = tar_bytes(("different", b"hello"))
        with self.assertRaisesRegex(ValueError, "missing regular file"):
            self.validate()

    def test_identical_duplicates(self) -> None:
        duplicate = self.root / "duplicate.deb"
        duplicate.write_bytes(self.package.read_bytes())
        vp.validate_packages([self.package, duplicate], "amd64")

    def test_conflicting_duplicates(self) -> None:
        duplicate = self.root / "duplicate.deb"
        duplicate.write_bytes(b"different bytes")
        with self.assertRaisesRegex(ValueError, "differing SHA256"):
            vp.validate_packages([self.package, duplicate], "amd64")

    def test_missing_tool_cli_error(self) -> None:
        self.tool.side_effect = FileNotFoundError("dpkg-deb not found")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = vp.main(["--arch", "amd64", str(self.package)])
        self.assertEqual(status, 1)
        self.assertIn("dpkg-deb not found", stderr.getvalue())
        self.assertIn(str(self.package), stderr.getvalue())

    def test_cli_success(self) -> None:
        self.assertEqual(vp.main(["--arch", "amd64", str(self.package)]), 0)

    def test_cli_requires_paths(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            vp.main(["--arch", "amd64"])
        self.assertEqual(error.exception.code, 2)

    def test_missing_package(self) -> None:
        self.package.unlink()
        with self.assertRaisesRegex(ValueError, "regular .deb file"):
            self.validate()

    def artifact_package(self, name: str) -> Path:
        path = self.root / name / "nested" / "sample.deb"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.package.read_bytes())
        return path.resolve()

    def test_artifacts_cross_arch_all_identity(self) -> None:
        self.artifact_package("deb-linux-amd64")
        arm = self.artifact_package("deb-linux-arm64")
        self.metadata["Architecture"] = "all"
        with self.assertRaisesRegex(ValueError, "produced by amd64 only"):
            vp.validate_artifacts(self.root)
        arm.write_bytes(b"different all package")
        with self.assertRaisesRegex(ValueError, "produced by amd64 only"):
            vp.validate_artifacts(self.root)

    def test_mixed_artifacts_with_single_all_producer(self) -> None:
        self.artifact_package("deb-linux-amd64")
        arm = self.artifact_package("deb-linux-arm64")

        def tool(command, **kwargs):
            if command[1] == "--field":
                self.metadata["Architecture"] = (
                    "arm64" if command[2] == str(arm) else "all"
                )
            return self.external_tool(command, **kwargs)

        self.tool.side_effect = tool
        vp.validate_artifacts(self.root)

    def test_artifacts_match_each_package_architecture(self) -> None:
        amd = self.artifact_package("deb-linux-amd64")
        arm = self.artifact_package("deb-linux-arm64")
        extra = self.artifact_package("deb-other-amd64")
        architectures = {str(amd): "amd64", str(arm): "arm64", str(extra): "amd64"}

        def tool(command, **kwargs):
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
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                other = "arm64" if arch == "amd64" else "amd64"
                (root / f"deb-linux-{other}").mkdir()
                (root / f"deb-linux-{other}" / "sample.deb").write_bytes(b"deb")
                for empty in (False, True):
                    if empty:
                        (root / f"deb-linux-{arch}").mkdir()
                    with self.assertRaisesRegex(ValueError, f"group: {arch}"):
                        vp.validate_artifacts(root)
        with self.assertRaisesRegex(ValueError, "empty or missing"):
            vp.validate_artifacts(self.root)

    def test_artifacts_reject_unknown_directory_names(self) -> None:
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
        with redirect_stderr(io.StringIO()):
            self.assertEqual(vp.main(["--artifacts", str(self.root / "absent")]), 1)

    def test_cli_rejects_mixed_modes(self) -> None:
        for arguments in (
            ["--artifacts", str(self.root), "--arch", "amd64"],
            ["--artifacts", str(self.root), str(self.package)],
            [],
        ):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    vp.main(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
