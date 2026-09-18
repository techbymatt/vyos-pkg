"""Ownership planning and fresh/restored output checks."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import package_build_policy as policy
except ImportError:
    import package_build_policy as policy


class PolicyTests(unittest.TestCase):
    def test_independent_sources_have_one_producer(self) -> None:
        for group, package in (
            ("build", "bash-completion"),
            ("build", "ddclient"),
            ("build", "pyhumps"),
            ("build", "waagent"),
            ("build-extra", "live-boot"),
            ("build-extra", "vyos-live-build"),
        ):
            with self.subTest(package=package):
                self.assertEqual(policy.architectures(group, package), ["amd64"])

    def test_mixed_and_arch_specific_sources_keep_native_builds(self) -> None:
        for package in ("net-snmp", "frr", "strongswan", "linux-kernel", "vyos-1x"):
            self.assertEqual(policy.architectures("build", package), ["amd64", "arm64"])
        self.assertEqual(policy.architectures("build", "shim-signed"), ["amd64"])

    def test_extra_matrix_combines_dependencies_without_duplicating_builds(
        self,
    ) -> None:
        entries = policy.matrix(
            "build-extra", ["live-boot", "hvinfo"], ["liba", "libb"]
        )["include"]
        self.assertEqual(
            [(e["package"], e["arch"]) for e in entries],
            [("live-boot", "amd64"), ("hvinfo", "amd64"), ("hvinfo", "arm64")],
        )
        self.assertTrue(all(e["deps"] == "liba libb" for e in entries))
        self.assertEqual(entries[-1]["runner_label"], "ubuntu-24.04-arm")
        self.assertEqual(policy.matrix("build", [], []), {"include": []})

    def test_output_policy_uses_control_metadata_not_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "misleading_arm64.deb").write_bytes(b"fixture")
            with patch.object(policy.subprocess, "check_output", return_value="all\n"):
                policy.check_outputs("build", "net-snmp", "amd64", directory)
                with self.assertRaisesRegex(ValueError, "Architecture"):
                    policy.check_outputs("build", "net-snmp", "arm64", directory)
                with self.assertRaisesRegex(ValueError, "no arm64 build"):
                    policy.check_outputs("build", "pyhumps", "arm64", directory)
            with (
                patch.object(policy.subprocess, "check_output", return_value="amd64\n"),
                self.assertRaisesRegex(ValueError, "expected.*all"),
            ):
                policy.check_outputs("build", "pyhumps", "amd64", directory)
            with (
                patch.object(
                    policy.subprocess,
                    "check_output",
                    side_effect=subprocess.CalledProcessError(2, "dpkg-deb"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                policy.check_outputs("build", "net-snmp", "amd64", directory)

    def test_empty_outputs_fail(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "no .deb outputs"),
        ):
            policy.check_outputs("build", "net-snmp", "arm64", Path(temporary))


if __name__ == "__main__":
    unittest.main()
