"""Build-mode adaptations, including optional checks against a VyOS checkout.

Set VYOS_BUILD_ROOT to a patched scripts/package-build directory to exercise
every adaptation against real upstream recipes (copies only; never builds them).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import tomllib

try:
    from . import prepare_package_build as prepare
except ImportError:
    import prepare_package_build as prepare


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
