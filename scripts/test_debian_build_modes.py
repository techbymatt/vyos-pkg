"""Exercise actual dpkg targets and binary metadata on a Debian build host."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from . import package_build_policy as policy
    from . import prepare_package_build as prepare
    from .test_prepare_package_build import (
        LEGACY_BIOSDEVNAME_RULES,
        udp_patch,
        write_udp_recipe,
    )
except ImportError:
    import package_build_policy as policy
    import prepare_package_build as prepare
    from test_prepare_package_build import (
        LEGACY_BIOSDEVNAME_RULES,
        udp_patch,
        write_udp_recipe,
    )


@unittest.skipUnless(
    shutil.which("dpkg-buildpackage") and shutil.which("make"),
    "requires Debian dpkg-dev and make",
)
class DebianBuildModeTests(unittest.TestCase):
    def test_udp_patch_repairs_native_binary_build(self) -> None:
        native = subprocess.check_output(
            ["dpkg", "--print-architecture"], text=True
        ).strip()
        for arch, mode in (("arm64", "any"), ("amd64", "binary")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                patch = write_udp_recipe(root, udp_patch())
                source = root / "source"
                source.mkdir()
                subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
                command = ["dpkg-buildpackage", f"--build={mode}", "-d", "-us", "-uc"]
                if mode == "any":
                    broken = subprocess.run(
                        command, check=False, cwd=source, text=True, capture_output=True
                    )
                    self.assertNotEqual(broken.returncode, 0)
                    self.assertIn("no binary artifacts found", broken.stderr)
                    self.assertEqual(list(root.glob("*.deb")), [])
                shutil.rmtree(source)
                source.mkdir()
                prepare.prepare(root, "udp-broadcast-relay", arch)
                subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
                fixed = subprocess.run(
                    command, check=False, cwd=source, text=True, capture_output=True
                )
                self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
                self.assertNotIn("must be updated to support", fixed.stderr)
                self.assertEqual(
                    [p.name for p in root.glob("*.deb")],
                    [f"udp-broadcast-relay_1.0-1_{native}.deb"],
                )
                policy.check_outputs("build", "udp-broadcast-relay", native, root)

    def test_legacy_arch_package_requires_repaired_binary_target(self) -> None:
        native = subprocess.check_output(
            ["dpkg", "--print-architecture"], text=True
        ).strip()
        for mode in ("any", "binary"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "vyatta-biosdevname"
                debian = source / "debian"
                debian.mkdir(parents=True)
                (debian / "control").write_text(
                    "Source: vyatta-biosdevname\nSection: misc\nPriority: optional\n"
                    "Maintainer: Tester <test@example.org>\nRules-Requires-Root: no\n\n"
                    "Package: vyatta-biosdevname\nArchitecture: any\nDescription: Native fixture\n"
                )
                (debian / "changelog").write_text(
                    "vyatta-biosdevname (1.0-1) unstable; urgency=low\n\n  * Fixture.\n\n"
                    " -- Tester <test@example.org>  Thu, 17 Sep 2026 00:00:00 +0000\n"
                )
                rules = debian / "rules"
                rules.write_text(LEGACY_BIOSDEVNAME_RULES)
                rules.chmod(0o755)
                command = ["dpkg-buildpackage", f"--build={mode}", "-d", "-us", "-uc"]
                if mode == "any":
                    broken = subprocess.run(
                        command,
                        cwd=source,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    self.assertNotEqual(broken.returncode, 0, broken.stdout)
                    self.assertIn("no binary artifacts found", broken.stdout)
                    self.assertEqual(list(root.glob("*.deb")), [])
                prepare.prepare_extra(root, "vyatta-biosdevname")
                fixed = subprocess.run(
                    command,
                    cwd=source,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(fixed.returncode, 0, fixed.stdout)
                self.assertNotIn("must be updated to support", fixed.stdout)
                self.assertEqual(
                    [p.name for p in root.glob("*.deb")],
                    [f"vyatta-biosdevname_1.0-1_{native}.deb"],
                )
                policy.check_outputs("build-extra", "vyatta-biosdevname", native, root)

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
