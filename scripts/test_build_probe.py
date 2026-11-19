"""Build-network inventory and probe tests; do not contact external services."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import build_probe_targets as targets
    from .test_package_sources import RecipeFixture, commit, git
except ImportError:
    import build_probe_targets as targets
    from test_package_sources import RecipeFixture, commit, git


class BuildProbeTests(unittest.TestCase):
    def test_recipe_inventory_uses_patched_tree_catalog_and_submodules(self):
        """Inventory only reads pinned Git objects; it never resolves remote refs."""
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RecipeFixture(Path(temporary))
            fixture.enable_kernel_dependencies()
            vpp = fixture.build_root / "vpp/package.toml"
            vpp.write_text(
                vpp.read_text().replace(
                    'name = "vpp"\nscm_url = "https://github.com/example/source.git"',
                    'name = "vpp"\nscm_url = "https://example.org/vpp.git"',
                )
            )
            kea = fixture.build_root / "isc-kea"
            kea.mkdir()
            (kea / "package.toml").write_text(
                '[[packages]]\nname = "isc-kea"\nscm_url = "https://example.org/kea"\n'
                'commit_id = "stable"\n'
            )
            (kea / "prebuild.sh").write_text(
                'BRANCH="Kea-3.0.3"\n'
                "git clone --branch ${BRANCH} https://example.org/kea-debian.git\n"
            )
            fixture.upstream_commit = commit(fixture.upstream)
            (fixture.patch_root / ".gitmodules").write_text(
                '[submodule "vyos-build"]\n path = vyos-build\n'
                " url = https://example.org/build.git\n"
            )
            git(fixture.patch_root, "add", ".gitmodules")
            config = (fixture.build_root / "frr/package.toml").read_text()
            fixture.add_patch(
                "scripts/package-build/frr/package.toml",
                config.replace(
                    "https://github.com/example/source.git",
                    "https://example.org/patched.git",
                ),
            )
            catalog = targets.package_catalog.validate_catalog(
                {
                    "build": [
                        {"name": name} for name in ("frr", "isc-kea", "linux-kernel")
                    ],
                    "build-extra": [{"name": "extra"}],
                }
            )
            with patch.object(
                targets.package_catalog, "load_catalog", return_value=catalog
            ):
                inventory = targets.recipe_targets(fixture.patch_root)
        self.assertIn(["git", "Git refs", "https://example.org/patched.git"], inventory)
        self.assertIn(["git", "Git refs", "https://example.org/build.git"], inventory)
        self.assertIn(["git", "Git refs", "https://example.org/vpp.git"], inventory)
        self.assertIn(
            ["git", "Git refs", "https://example.org/kea-debian.git"], inventory
        )
        self.assertIn(
            ["git", "Git refs", "https://github.com/vyos/extra.git"], inventory
        )
        self.assertIn(
            [
                "get",
                "Kernel signature",
                "https://www.kernel.org/pub/linux/kernel/v6.x/linux-6.18.50.tar.sign",
            ],
            inventory,
        )
        self.assertEqual(len(inventory), len({(row[0], row[2]) for row in inventory}))

    def test_apt_inventory_uses_configured_indexes_without_fetching(self):
        with patch.object(
            targets,
            "command_output",
            return_value=(
                "'http://mirror.example/debian/dists/bookworm/InRelease' file 0\n"
                "'http://mirror.example/debian/dists/bookworm/main/Packages.xz' packages 0\n"
            ),
        ) as command:
            self.assertEqual(
                targets.apt_targets(),
                [
                    [
                        "get",
                        "APT index",
                        "http://mirror.example/debian/dists/bookworm/InRelease",
                    ],
                    [
                        "head",
                        "APT index",
                        "http://mirror.example/debian/dists/bookworm/main/Packages.xz",
                    ],
                ],
            )
        command.assert_called_once_with(
            ["apt-get", "--print-uris", "update"], timeout=20, env={"LC_ALL": "C"}
        )

    def test_rejects_bad_urls_before_reporting_or_probing(self):
        for url in (
            "file:///etc/passwd",
            "https://user:password@example.org",
            "https://example.org/a|b",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                targets.validate_target(["get", "fixture", url])

    def test_probe_preserves_failed_rows_and_scopes_private_authentication(self):
        script = Path(__file__).with_name("probe_build_network.sh")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary, inventory, curl = (
                root / name for name in ("summary", "targets", "curl")
            )
            inventory.write_text(
                "git\tPrivate\thttps://github.com/techbymatt/tbm-vyos-patch.git\n"
                "head\tArchive\thttps://example.org/file.tar.xz\n"
                "get\tWKD direct policy\thttps://kernel.org/.well-known/openpgpkey/policy\n"
            )
            curl.write_text(
                """#!/bin/bash
set -eu
[[ "$1" != --version ]] || exit 0
args=" $* "
case "${@: -1}" in
  'https://github.com/techbymatt/tbm-vyos-patch.git/info/refs?service=git-upload-pack')
    [[ "$args" == *' --netrc-file '* && "$args" != *' --location '* && "$args" != *' --verbose '* ]] || exit 99 ;;
  'https://example.org/file.tar.xz')
    [[ "$args" == *' --head '* && "$args" == *' --location '* && "$args" != *' --netrc-file '* ]] || exit 99 ;;
  'https://kernel.org/.well-known/openpgpkey/policy')
    [[ "$args" != *' --head '* && "$args" != *' --location '* && "$args" != *' --netrc-file '* ]] || exit 99 ;;
  *) exit 99 ;;
esac
echo 'Authorization: do not disclose' >&2
case "$2" in
  -4) echo '*   Trying 192.0.2.8:443...' >&2
      echo '192.0.2.8 | 200 | 0.01 | 0.02 | 0.03 | 0.04 | 0' ;;
  -6) echo '*   Trying [2001:db8::8]:443...' >&2
      echo ' | 000 | 0.01 | 0.00 | 0.00 | 20.00 | 0'
      exit 28 ;;
  *) exit 99 ;;
esac
"""
            )
            curl.chmod(0o755)
            result = subprocess.run(
                ["bash", str(script), str(inventory)],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "PATCH_TOKEN": "fixture-token",
                    "RECIPE_INVENTORY": "failure",
                },
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            report = summary.read_text()
        self.assertEqual(report.count("| IPv4 | 0 | 192.0.2.8 | 192.0.2.8 | 200 |"), 3)
        self.assertEqual(report.count("| IPv6 | 28 | [2001:db8::8] |  | 000 |"), 3)
        self.assertIn("**Partial inventory:**", report)
        self.assertNotIn("do not disclose", result.stdout + result.stderr + report)
        self.assertNotIn("fixture-token", result.stdout + result.stderr + report)


if __name__ == "__main__":
    unittest.main()
