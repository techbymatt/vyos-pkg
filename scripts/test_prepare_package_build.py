"""Build-mode adaptations, including optional checks against a VyOS checkout.

Set VYOS_BUILD_ROOT to a patched scripts/package-build directory to exercise
every adaptation against real upstream recipes (copies only; never builds them).
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import prepare_package_build as prepare
except ImportError:
    import prepare_package_build as prepare


# Command layout from vyos-build 0bb5f2bc, with a separate dependency/split-horizon hook.
FRR_RECIPE = r"""[[packages]]
name = "libyang"
commit_id = "v3.13.6"
scm_url = "https://github.com/CESNET/libyang.git"
build_cmd = "pipx run apkg build -i && find pkg/pkgs -type f -name *.deb -exec mv -t .. {} + && sudo dpkg -i ../libyang*.deb"

[[packages]]
name = "libbpf"
commit_id = "debian/1.7.0-1_exp1"
scm_url = "https://salsa.debian.org/sudip/libbpf.git"

[[packages]]
name = "frr"
commit_id = "9d7b378b80fed"
scm_url = "https://github.com/FRRouting/frr.git"
pre_build_hook = "sudo dpkg -i ../*.deb && ./enable-splithorizon"
build_cmd = "dpkg-buildpackage -us -uc -tc -F --source-option=--tar-ignore=.git --source-option=--tar-ignore=.github --source-option=-i --source-option=--extend-diff-ignore='^\\.github(?:/.*)?$' -Ppkg.frr.rtrlib,pkg.frr.lua"

[dependencies]
packages = ["librtr-dev", "liblua5.3-dev", "libbpf-dev"]
"""

KEY_LOOKUP = (
    "run(['gpg2', '--locate-keys', 'torvalds@kernel.org', "
    "'gregkh@kernel.org'], check=True)"
)
KERNEL_BUILDER = """import subprocess
import sys
from subprocess import run, CalledProcessError

def build_kernel(source_exists=False):
    if not source_exists:
        KEY_LOOKUP
        run(['curl', '-OL', 'linux.tar.xz'], check=True)
        run(['gpg2', '--verify', 'linux.tar.sign', '-'], check=True)
        run(['tar', 'xf', 'linux.tar.xz'], check=True)

def build_packages():
    try:
        build_kernel()
    except CalledProcessError:
        pass  # Matches upstream's catch-and-continue package loop.
    run(['./build-linux-firmware.sh'], check=True)
""".replace("KEY_LOOKUP", KEY_LOOKUP)


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
    """Build the fixture Debian-packaging patch for udp-broadcast-relay."""
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
    """Write a minimal udp-broadcast-relay recipe and return the patch path."""
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
    """Tests for the udp-broadcast-relay packaging adaptation."""

    def test_adapted_patch_applies_and_selects_native_packaging(self) -> None:
        """The rewritten patch applies and picks native dpkg packaging per arch."""
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
        """Unexpected patch variants fail without editing the patch file."""
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
    """Tests for build-extra sources that bypass the shared builder."""

    def setUp(self) -> None:
        """Create a scratch tree with a legacy vyatta-biosdevname recipe."""
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
        """The CLI adapts extra sources without invoking the shared builder."""
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
        """Preparing an unrelated extra source leaves other recipes untouched."""
        prepare.prepare_extra(self.root, "live-boot")
        self.assertEqual(self.rules.read_text(), LEGACY_BIOSDEVNAME_RULES)

    def test_rule_drift_fails_without_partial_edits(self) -> None:
        """Drifted rules fail without partially editing them."""
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
        """Unexpected control content fails without editing the recipe."""
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
    """Tests for shared-builder recipe adaptations."""

    def setUp(self) -> None:
        """Create a scratch root containing the shared build.py."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "build.py").write_text(
            "dpkg-buildpackage -uc -us -tc -F --source-option\n"
        )

    def recipe(self, package: str, command: str) -> Path:
        """Write a package.toml recipe stub for a package."""
        directory = self.root / package
        directory.mkdir()
        path = directory / "package.toml"
        path.write_text(command)
        return path

    def test_arm64_net_snmp_excludes_independent_outputs(self) -> None:
        """arm64 net-snmp drops independent outputs and builds source,any."""
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
        """amd64 keeps independent outputs and full binary builds."""
        path = self.recipe(
            "net-snmp", 'build_cmd = "dpkg-buildpackage -us -uc -tc -b || true"\n'
        )
        prepare.prepare(self.root, "net-snmp", "amd64")
        self.assertIn("--build=binary", path.read_text())
        self.assertIn("--build=full", (self.root / "build.py").read_text())

    def test_vici_is_not_built_on_arm64(self) -> None:
        """The vici build step is stripped from arm64 strongswan builds."""
        path = self.recipe(
            "strongswan",
            "dpkg-buildpackage -uc -us -tc -b -d\ncd ..; ./build-vici.sh\n",
        )
        prepare.prepare(self.root, "strongswan", "arm64")
        self.assertNotIn("./build-vici.sh", path.read_text())
        self.assertIn("--build=any -d", path.read_text())

    def test_frr_preserves_source_builds_and_libyang_native_modes(self) -> None:
        """FRR keeps source checks/options; only amd64 builds independent binaries."""
        path = self.recipe("frr", FRR_RECIPE)
        builder = self.root / "build.py"
        original_builder = builder.read_text()
        for arch, source_mode, binary_mode in (
            ("amd64", "full", "binary"),
            ("arm64", "source,any", "any"),
        ):
            with self.subTest(arch=arch):
                path.write_text(FRR_RECIPE)
                builder.write_text(original_builder)
                expected = tomllib.loads(FRR_RECIPE)
                expected["packages"][0]["build_cmd"] = (
                    "pipx run apkg build-dep && "
                    "pipx run apkg srcpkg --result-dir apkg-source && "
                    "dpkg-source -x apkg-source/*.dsc ../libyang-build && "
                    "(cd ../libyang-build && "
                    f"dpkg-buildpackage --build={binary_mode} -us -uc -tc) && "
                    "sudo dpkg -i ../libyang*.deb"
                )
                frr = expected["packages"][2]
                frr["build_cmd"] = frr["build_cmd"].replace(
                    " -F ", f" --build={source_mode} "
                )
                prepare.prepare(self.root, "frr", arch)
                self.assertEqual(tomllib.loads(path.read_text()), expected)
                self.assertEqual(
                    builder.read_text(),
                    original_builder.replace(" -F ", f" --build={source_mode} "),
                )

    def test_frr_command_drift_fails_without_partial_edits(self) -> None:
        """Missing, duplicate, or changed commands leave the recipe/builder intact."""
        path = self.recipe("frr", FRR_RECIPE)
        builder = self.root / "build.py"
        original_builder = builder.read_text()
        frr_command = next(
            line
            for line in FRR_RECIPE.splitlines()
            if line.startswith('build_cmd = "dpkg-buildpackage')
        )
        variants = (
            ("missing FRR command", FRR_RECIPE.replace(frr_command, "")),
            (
                "binary-only FRR command",
                FRR_RECIPE.replace(
                    frr_command,
                    'build_cmd = "dpkg-buildpackage -us -uc -tc -b '
                    '-Ppkg.frr.rtrlib,pkg.frr.lua"',
                ),
            ),
            (
                "duplicate FRR command",
                FRR_RECIPE + f'\n[[packages]]\nname = "duplicate"\n{frr_command}\n',
            ),
            (
                "changed source options",
                FRR_RECIPE.replace("--source-option=--tar-ignore=.github ", ""),
            ),
            (
                "changed build profiles",
                FRR_RECIPE.replace("pkg.frr.lua", "pkg.frr.other"),
            ),
            (
                "changed libyang command",
                FRR_RECIPE.replace("pipx run apkg build -i", "pipx run apkg build"),
            ),
        )
        for arch in ("amd64", "arm64"):
            for label, text in variants:
                with self.subTest(arch=arch, variant=label):
                    path.write_text(text)
                    builder.write_text(original_builder)
                    with self.assertRaisesRegex(
                        ValueError, "expected exactly one audited command"
                    ):
                        prepare.prepare(self.root, "frr", arch)
                    self.assertEqual(path.read_text(), text)
                    self.assertEqual(builder.read_text(), original_builder)

    def test_unrecognized_recipe_change_fails(self) -> None:
        """Unrecognized recipe commands fail instead of being edited."""
        self.recipe("net-snmp", 'build_cmd = "different command"\n')
        with self.assertRaisesRegex(ValueError, "expected exactly one audited command"):
            prepare.prepare(self.root, "net-snmp", "arm64")
        self.assertEqual(
            (self.root / "build.py").read_text(),
            "dpkg-buildpackage -uc -us -tc -F --source-option\n",
        )

    def test_multiple_source_commands_are_checked_from_the_registry(self) -> None:
        """The three tacacs dependency builds retain order and share one native mode."""
        command = "dpkg-buildpackage -us -uc -tc -b"
        path = self.recipe("tacacs", "\n".join([command] * 3))
        prepare.prepare(self.root, "tacacs", "arm64")
        self.assertEqual(path.read_text().count("--build=any"), 3)
        self.assertNotIn(" -b", path.read_text())

    def test_invalid_package_paths_and_architectures_fail_before_shared_edits(
        self,
    ) -> None:
        """Direct helper callers receive the same safe input contract as the CLI."""
        before = (self.root / "build.py").read_text()
        for package, arch in (("../escape", "amd64"), ("frr", "riscv64")):
            with (
                self.subTest(package=package, arch=arch),
                self.assertRaises(ValueError),
            ):
                prepare.prepare(self.root, package, arch)
        self.assertEqual((self.root / "build.py").read_text(), before)


class KernelPrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = self.root / "build.py"
        self.shared.write_text("dpkg-buildpackage -uc -us -tc -F --source-option\n")
        self.builder = self.root / "linux-kernel/build.py"
        self.builder.parent.mkdir()
        self.builder.write_text(KERNEL_BUILDER)

    def kernel_fixture(self):
        helper = mock.Mock()
        self.enterContext(mock.patch.dict(sys.modules, {"kernel_keys": helper}))
        prepare.prepare(self.root, "linux-kernel", "arm64")
        builder = self.builder.read_text()
        verify = "run(['gpg2', '--verify', 'linux.tar.sign', '-'], check=True)"
        self.assertIn(verify, builder)
        self.assertNotIn(KEY_LOOKUP, builder)
        spec = importlib.util.spec_from_file_location("kernel_fixture", self.builder)
        if spec is None or spec.loader is None:
            raise ValueError("fixture builder cannot be imported")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        command = mock.Mock()
        module.run = command
        return module.build_packages, helper.locate_kernel_keys, command

    def test_lookup_success_leaves_signature_verification_in_place(self):
        build, locate, run = self.kernel_fixture()
        build()
        locate.assert_called_once_with("torvalds@kernel.org", "gregkh@kernel.org")
        run.assert_any_call(["gpg2", "--verify", "linux.tar.sign", "-"], check=True)

    def test_lookup_failure_stops_before_download_and_dependent_builds(self):
        build, locate, run = self.kernel_fixture()
        locate.side_effect = subprocess.CalledProcessError(2, ["gpg2", "--locate-keys"])
        with self.assertRaisesRegex(SystemExit, "Failed to locate kernel signing keys"):
            build()
        run.assert_not_called()

    def test_lookup_command_drift_leaves_both_builders_unmodified(self):
        drifted = KERNEL_BUILDER.replace("'--locate-keys'", "'--locate-external-keys'")
        self.builder.write_text(drifted)
        original_shared = self.shared.read_text()
        with self.assertRaisesRegex(ValueError, "audited command"):
            prepare.prepare(self.root, "linux-kernel", "amd64")
        self.assertEqual(self.shared.read_text(), original_shared)
        self.assertEqual(self.builder.read_text(), drifted)


@unittest.skipUnless(
    os.environ.get("VYOS_BUILD_ROOT"), "optional upstream recipe checkout"
)
class UpstreamRecipeTests(unittest.TestCase):
    """Optional audit of adaptations against a real VyOS checkout."""

    def test_audited_adaptations_apply_to_real_recipes(self) -> None:
        """All audited adaptations apply cleanly to real upstream recipes."""
        source = Path(os.environ["VYOS_BUILD_ROOT"])
        packages = tuple(
            dict.fromkeys(
                [
                    *prepare.BINARY_COMMANDS,
                    *prepare.RECIPE_HOOKS,
                    "vyos-1x",
                ]
            )
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
                    if package == "linux-kernel":
                        builder = root / package / "build.py"
                        compile(builder.read_text(), str(builder), "exec")


if __name__ == "__main__":
    unittest.main()
