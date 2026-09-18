"""Build-mode adaptations, including optional checks against a VyOS checkout.

Set VYOS_BUILD_ROOT to a patched scripts/package-build directory to exercise
every adaptation against real upstream recipes (copies only; never builds them).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib

try:
    from . import prepare_package_build as prepare
except ImportError:
    import prepare_package_build as prepare


# Minimal legacy rules reproducing the upstream target layout, with real dpkg
# packaging commands so Debian tests can exercise the failure and the repair.
LEGACY_BIOSDEVNAME_RULES = """#!/usr/bin/make -f
build: build-stamp
build-stamp:
\ttouch $@
clean:
\trm -rf debian/vyatta-biosdevname debian/files build-stamp
install: build
\tmkdir -p debian/vyatta-biosdevname/DEBIAN
# Build architecture-independent files here.
binary-indep: build install
\tdpkg-gencontrol -pvyatta-biosdevname -Pdebian/vyatta-biosdevname
\tdpkg-deb --build --root-owner-group debian/vyatta-biosdevname ..
# Build architecture-dependent files here.
binary-arch: build install
# This is an architecture independent package
# so; we have nothing to do by default.
binary: binary-indep binary-arch
.PHONY: build clean binary-indep binary-arch binary install
"""


# Target layout from VyOS's 0001-Add-Debian-packaging.patch. The commands use
# dpkg directly so this fixture needs no debhelper on Debian test hosts.
LEGACY_UDP_RULES = """#!/usr/bin/make -f
build: build-stamp
build-stamp:
\ttouch $@
clean:
\trm -rf debian/udp-broadcast-relay debian/files build-stamp
install: build
\tmkdir -p debian/udp-broadcast-relay/DEBIAN
# Build architecture-independent files here.
binary-indep: build install
\tdpkg-gencontrol -pudp-broadcast-relay -Pdebian/udp-broadcast-relay
\tdpkg-deb --build --root-owner-group debian/udp-broadcast-relay ..
# Build architecture-dependent files here.
binary-arch: build install
# This is an architecture independent package
# so; we have nothing to do by default.
binary: binary-indep
.PHONY: build clean binary-indep binary install
"""


def udp_patch(rules: str = LEGACY_UDP_RULES) -> str:
    control = (
        "Source: udp-broadcast-relay\nSection: net\nPriority: optional\n"
        "Maintainer: Tester <test@example.org>\nRules-Requires-Root: no\n\n"
        "Package: udp-broadcast-relay\nArchitecture: linux-any\nDescription: Native fixture\n"
    )
    changelog = (
        "udp-broadcast-relay (1.0-1) unstable; urgency=low\n\n  * Fixture.\n\n"
        " -- Tester <test@example.org>  Thu, 17 Sep 2026 00:00:00 +0000\n"
    )
    patch = "From: Tester <test@example.org>\nSubject: [PATCH] Add Debian packaging\n\n"
    for name, contents in (
        ("control", control),
        ("rules", rules),
        ("changelog", changelog),
    ):
        mode = "100755" if name == "rules" else "100644"
        patch += (
            f"diff --git a/debian/{name} b/debian/{name}\nnew file mode {mode}\n"
            f"--- /dev/null\n+++ b/debian/{name}\n"
            f"@@ -0,0 +1,{len(contents.splitlines())} @@\n"
            + "".join("+" + line for line in contents.splitlines(keepends=True))
        )
    return patch


def write_udp_recipe(root: Path, patch: str) -> Path:
    directory = root / "udp-broadcast-relay"
    patches = directory / "patches/udp-broadcast-relay"
    patches.mkdir(parents=True)
    path = patches / "0001-Add-Debian-packaging.patch"
    path.write_text(patch)
    (root / "build.py").write_text("dpkg-buildpackage -uc -us -tc -F --source-option\n")
    (directory / "package.toml").write_text(
        'build_cmd = "dpkg-buildpackage -uc -us -tc -b -d"\n'
    )
    return path


class UdpPrepareTests(unittest.TestCase):
    def test_adapted_patch_applies_and_selects_native_packaging(self) -> None:
        for arch, mode in (("amd64", "binary"), ("arm64", "any")):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = write_udp_recipe(root, udp_patch())
                prepare.prepare(root, "udp-broadcast-relay", arch)
                source = root / "source"
                source.mkdir()
                subprocess.run(["git", "apply", str(path)], cwd=source, check=True)
                rules = (source / "debian/rules").read_text()
                self.assertIn("binary-arch: build install\n\tdpkg-gencontrol", rules)
                self.assertIn("binary-indep:\nbinary: binary-arch binary-indep", rules)
                self.assertIn("build-arch: build-stamp", rules)
                self.assertIn("build-indep:\n", rules)
                self.assertIn(
                    f"--build={mode}",
                    (root / "udp-broadcast-relay/package.toml").read_text(),
                )
                # git apply must also preserve the adjacent hunks and executable bit.
                self.assertIn(
                    "Architecture: linux-any", (source / "debian/control").read_text()
                )
                self.assertTrue((source / "debian/changelog").exists())
                self.assertTrue((source / "debian/rules").stat().st_mode & 0o111)

    def test_patch_drift_fails_without_modifying_patch(self) -> None:
        original = udp_patch()
        variants = (
            original.replace("Architecture: linux-any", "Architecture: all"),
            original.replace(
                "+Architecture: linux-any", "+Architecture: linux-any\n+Package: extra"
            ),
            udp_patch(
                LEGACY_UDP_RULES.replace("binary: binary-indep", "binary: binary-arch")
            ),
            udp_patch(LEGACY_UDP_RULES + "build-arch: build\n"),
            original.replace("+++ b/debian/rules", "+++ b/debian/other"),
            original.replace("+build: build-stamp\n", ""),
        )
        for patch in variants:
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = write_udp_recipe(root, patch)
                with self.assertRaises(ValueError):
                    prepare.prepare_udp_packaging(root / "udp-broadcast-relay")
                self.assertEqual(path.read_text(), patch)


class ExtraPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.debian = self.root / "vyatta-biosdevname" / "debian"
        self.debian.mkdir(parents=True)
        (self.debian / "control").write_text(
            "Source: vyatta-biosdevname\n\nPackage: vyatta-biosdevname\nArchitecture: any\n"
        )
        self.rules = self.debian / "rules"
        self.rules.write_text(LEGACY_BIOSDEVNAME_RULES)

    def test_cli_handles_extra_sources_without_shared_builder(self) -> None:
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                self.rules.write_text(LEGACY_BIOSDEVNAME_RULES)
                subprocess.run(
                    [
                        sys.executable,
                        prepare.__file__,
                        "--group",
                        "build-extra",
                        "--root",
                        str(self.root),
                        "--package",
                        "vyatta-biosdevname",
                        "--arch",
                        arch,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                result = self.rules.read_text()
                self.assertIn("build-arch: build-stamp\n", result)
                self.assertIn("build-indep:\n", result)
                self.assertIn("binary-arch: build install\n\tdpkg-gencontrol", result)
                self.assertIn("binary-indep:\nbinary:", result)
                self.assertNotIn("independent package\n# so;", result)

    def test_other_extra_packages_are_untouched(self) -> None:
        prepare.prepare_extra(self.root, "live-boot")
        self.assertEqual(self.rules.read_text(), LEGACY_BIOSDEVNAME_RULES)

    def test_rule_drift_fails_without_partial_edits(self) -> None:
        for changed in (
            LEGACY_BIOSDEVNAME_RULES.replace(
                "binary-arch: build install", "binary-arch: install"
            ),
            LEGACY_BIOSDEVNAME_RULES + "build-arch: build\n",
            LEGACY_BIOSDEVNAME_RULES.replace(
                ".PHONY: build clean", ".PHONY: clean build"
            ),
        ):
            with self.subTest(rules=changed):
                self.rules.write_text(changed)
                with self.assertRaises(ValueError):
                    prepare.prepare_extra(self.root, "vyatta-biosdevname")
                self.assertEqual(self.rules.read_text(), changed)

    def test_control_drift_fails_without_edits(self) -> None:
        for control in (
            "Package: vyatta-biosdevname\nArchitecture: all\n",
            "Package: vyatta-biosdevname\nArchitecture: any\n\nPackage: new-data\nArchitecture: all\n",
        ):
            with self.subTest(control=control):
                (self.debian / "control").write_text(control)
                with self.assertRaisesRegex(
                    ValueError, "expected one Architecture: any"
                ):
                    prepare.prepare_extra(self.root, "vyatta-biosdevname")
                self.assertEqual(self.rules.read_text(), LEGACY_BIOSDEVNAME_RULES)


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "build.py").write_text(
            "dpkg-buildpackage -uc -us -tc -F --source-option\n"
        )

    def recipe(self, package: str, command: str) -> Path:
        directory = self.root / package
        directory.mkdir()
        path = directory / "package.toml"
        path.write_text(command)
        return path

    def test_arm64_net_snmp_excludes_independent_outputs(self) -> None:
        path = self.recipe(
            "net-snmp", 'build_cmd = "dpkg-buildpackage -us -uc -tc -b || true"\n'
        )
        prepare.prepare(self.root, "net-snmp", "arm64")
        self.assertEqual(
            tomllib.loads(path.read_text())["build_cmd"],
            "dpkg-buildpackage -us -uc -tc --build=any || true",
        )
        self.assertIn("--build=source,any", (self.root / "build.py").read_text())

    def test_amd64_retains_independent_outputs(self) -> None:
        path = self.recipe(
            "net-snmp", 'build_cmd = "dpkg-buildpackage -us -uc -tc -b || true"\n'
        )
        prepare.prepare(self.root, "net-snmp", "amd64")
        self.assertIn("--build=binary", path.read_text())
        self.assertIn("--build=full", (self.root / "build.py").read_text())

    def test_vici_is_not_built_on_arm64(self) -> None:
        path = self.recipe(
            "strongswan",
            "dpkg-buildpackage -uc -us -tc -b -d\ncd ..; ./build-vici.sh\n",
        )
        prepare.prepare(self.root, "strongswan", "arm64")
        self.assertNotIn("./build-vici.sh", path.read_text())
        self.assertIn("--build=any -d", path.read_text())

    def test_libyang_uses_rendered_source_with_explicit_binary_mode(self) -> None:
        path = self.recipe(
            "frr",
            'build_cmd = "pipx run apkg build -i && find pkg/pkgs -type f -name *.deb -exec mv -t .. {} +"\n'
            'frr = "dpkg-buildpackage -us -uc -tc -b -Ppkg.frr.rtrlib,pkg.frr.lua"\n',
        )
        prepare.prepare(self.root, "frr", "arm64")
        command = tomllib.loads(path.read_text())["build_cmd"]
        self.assertIn("apkg build-dep && pipx run apkg srcpkg", command)
        self.assertIn("dpkg-source -x", command)
        self.assertIn("dpkg-buildpackage --build=any", command)
        self.assertNotIn("apkg build -i", command)

    def test_unrecognized_recipe_change_fails(self) -> None:
        self.recipe("net-snmp", 'build_cmd = "different command"\n')
        with self.assertRaisesRegex(ValueError, "expected exactly one audited command"):
            prepare.prepare(self.root, "net-snmp", "arm64")


@unittest.skipUnless(
    os.environ.get("VYOS_BUILD_ROOT"), "optional upstream recipe checkout"
)
class UpstreamRecipeTests(unittest.TestCase):
    def test_audited_adaptations_apply_to_real_recipes(self) -> None:
        source = Path(os.environ["VYOS_BUILD_ROOT"])
        packages = (
            "dropbear",
            "frr",
            "net-snmp",
            "netfilter",
            "openssl",
            "openvpn",
            "strongswan",
            "tacacs",
            "udp-broadcast-relay",
            "xen-guest-agent",
            "hostap",
            "linux-kernel",
            "vyos-1x",
        )
        for arch in ("amd64", "arm64"):
            for package in packages:
                with (
                    self.subTest(arch=arch, package=package),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    shutil.copy2(source / "build.py", root / "build.py")
                    shutil.copytree(source / package, root / package, symlinks=True)
                    prepare.prepare(root, package, arch)
                    if package == "udp-broadcast-relay":
                        # Apply just the packaging hunk to a disposable source tree;
                        # this checks the rewritten real patch's counts and targets.
                        unpacked = root / "unpacked"
                        unpacked.mkdir()
                        patch = (
                            root
                            / package
                            / "patches/udp-broadcast-relay/0001-Add-Debian-packaging.patch"
                        )
                        subprocess.run(
                            ["git", "apply", "--include=debian/*", str(patch)],
                            cwd=unpacked,
                            check=True,
                        )
                        rules = (unpacked / "debian/rules").read_text()
                        self.assertIn(
                            "binary-arch: build install\n\trm -f debian/files", rules
                        )
                        self.assertIn("binary: binary-arch binary-indep", rules)
                    tomllib.loads((root / package / "package.toml").read_text())
                    compile((root / "build.py").read_text(), "build.py", "exec")


if __name__ == "__main__":
    unittest.main()
