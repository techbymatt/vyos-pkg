"""Exercise actual dpkg targets and binary metadata on a Debian build host."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from . import package_build_policy as policy
except ImportError:
    import package_build_policy as policy


@unittest.skipUnless(
    shutil.which("dpkg-buildpackage") and shutil.which("make"),
    "requires Debian dpkg-dev and make",
)
class DebianBuildModeTests(unittest.TestCase):
    def test_mixed_source_builds_independent_package_only_in_binary_mode(self) -> None:
        native = subprocess.check_output(
            ["dpkg", "--print-architecture"], text=True
        ).strip()
        for mode in ("binary", "any"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                debian = source / "debian"
                debian.mkdir(parents=True)
                (debian / "control").write_text(
                    "Source: sample\nSection: misc\nPriority: optional\n"
                    "Maintainer: Tester <test@example.org>\nRules-Requires-Root: no\n\n"
                    "Package: sample-native\nArchitecture: any\nDescription: Native fixture\n\n"
                    "Package: sample-common\nArchitecture: all\nDescription: Independent fixture\n"
                )
                (debian / "changelog").write_text(
                    "sample (1.0-1) unstable; urgency=low\n\n  * Fixture.\n\n"
                    " -- Tester <test@example.org>  Thu, 17 Sep 2026 00:00:00 +0000\n"
                )
                rules = debian / "rules"
                rules.write_text(
                    "#!/usr/bin/make -f\n"
                    "build build-arch build-indep clean:\n\t@true\n"
                    "binary: binary-arch binary-indep\n"
                    "binary-arch:\n"
                    "\tmkdir -p debian/sample-native/DEBIAN\n"
                    "\tdpkg-gencontrol -psample-native -Pdebian/sample-native\n"
                    "\tdpkg-deb --build --root-owner-group debian/sample-native ..\n"
                    "binary-indep:\n"
                    "\tmkdir -p debian/sample-common/DEBIAN\n"
                    "\tdpkg-gencontrol -psample-common -Pdebian/sample-common\n"
                    "\tdpkg-deb --build --root-owner-group debian/sample-common ..\n"
                )
                rules.chmod(0o755)
                result = subprocess.run(
                    ["dpkg-buildpackage", f"--build={mode}", "-d", "-us", "-uc"],
                    cwd=source,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                identities = {
                    tuple(
                        subprocess.check_output(
                            [
                                "dpkg-deb",
                                "--show",
                                "--showformat=${Package} ${Architecture}",
                                str(path),
                            ],
                            text=True,
                        ).split()
                    )
                    for path in root.glob("*.deb")
                }
                expected = {("sample-native", native)}
                if mode == "binary":
                    expected.add(("sample-common", "all"))
                self.assertEqual(identities, expected)
                if mode == "binary" and native == "arm64":
                    with self.assertRaisesRegex(ValueError, "Architecture"):
                        policy.check_outputs("build", "sample", native, root)
                else:
                    policy.check_outputs("build", "sample", native, root)


if __name__ == "__main__":
    unittest.main()
