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
                    tomllib.loads((root / package / "package.toml").read_text())
                    compile((root / "build.py").read_text(), "build.py", "exec")


if __name__ == "__main__":
    unittest.main()
