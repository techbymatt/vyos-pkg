"""Catalog membership, dependency migration and strict schema validation."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from pathlib import Path

try:
    from . import package_catalog as catalog
except ImportError:
    import package_catalog as catalog


class CatalogTests(unittest.TestCase):
    """Tests for catalog loading and strict validation."""

    def test_catalog_preserves_publish_sources(self) -> None:
        """Catalog groups keep their pinned source names and dependencies."""
        sources = catalog.load_catalog()
        self.assertEqual(
            [entry["name"] for entry in sources["build"]],
            [
                "amazon-cloudwatch-agent",
                "amazon-ssm-agent",
                "aws-gwlbtun",
                "bash-completion",
                "blackbox_exporter",
                "ddclient",
                "dropbear",
                "ethtool",
                "frr",
                "frr_exporter",
                "hostap",
                "hsflowd",
                "iproute2",
                "isc-dhcp",
                "isc-kea",
                "keepalived",
                "libhtp",
                "libnss-mapuser",
                "libpam-radius-auth",
                "linux-kernel",
                "ndppd",
                "net-snmp",
                "netfilter",
                "node_exporter",
                "openssl",
                "openvpn-otp",
                "openvpn",
                "owamp",
                "podman",
                "pyhumps",
                "radvd",
                "shim-signed",
                "squid",
                "strongswan",
                "tacacs",
                "telegraf",
                "udp-broadcast-relay",
                "vyatta-bash",
                "vyos-1x",
                "waagent",
                "wide-dhcpv6",
                "xen-guest-agent",
                "zerotier-one",
            ],
        )
        self.assertEqual(
            {
                entry["name"]: " ".join(entry["deps"])
                for entry in sources["build-extra"]
            },
            {
                "hvinfo": "gnat gprbuild",
                "ipaddrcheck": "check libcidr-dev",
                "libnss-tacplus": "libaudit-dev libpam-tacplus-dev libtac-dev libtacplus-map-dev",
                "libpam-tacplus": "autoconf-archive libaudit-dev libpam-dev libssl-dev libtacplus-map-dev",
                "libtacplus-map": "autoconf-archive libaudit-dev",
                "live-boot": "",
                "vyatta-biosdevname": "libpci-dev",
                "vyatta-cfg": "bison flex libboost-filesystem-dev libglib2.0-dev",
                "vyos-http-api-tools": "dh-virtualenv",
                "vyos-live-build": "",
            },
        )

    def test_architecture_distinctions_and_unknown_sources(self) -> None:
        """Architecture policies are per source and group, with dual defaults."""
        self.assertEqual(
            catalog.architecture_policy("build", "pyhumps"), "independent_only"
        )
        self.assertEqual(
            catalog.architecture_policy("build", "shim-signed"), "amd64_only"
        )
        for group in catalog.GROUPS:
            self.assertEqual(
                catalog.architectures(group, "new-source"), ["amd64", "arm64"]
            )
        self.assertEqual(
            catalog.architectures("build-extra", "pyhumps"), ["amd64", "arm64"]
        )

    def test_invalid_catalog_entries_fail_closed(self) -> None:
        """Malformed or unsafe entries, duplicates, and missing groups raise."""
        for entry in (
            {"name": "../escape"},
            {"name": "valid", "architecture": "arm64"},
            {"name": "valid", "deps": "gnat"},
            {"name": "valid", "deps": ["$(id)"]},
            {"name": "valid", "deps": ["gnat", "gnat"]},
            {"name": "valid", "typo": True},
            {"deps": []},
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                catalog.validate_catalog({"build": [], "build-extra": [entry]})
        original = {
            "build": [{"name": "duplicate"}],
            "build-extra": [{"name": "duplicate"}],
        }
        with self.assertRaisesRegex(ValueError, "duplicate catalog source"):
            catalog.validate_catalog(original)
        with self.assertRaises(ValueError):
            catalog.validate_catalog(
                {"build": [{"name": "valid", "deps": ["gnat"]}], "build-extra": []}
            )

    def test_validation_returns_normalized_copy(self) -> None:
        """Validation fills defaults without mutating the original input."""
        original = {"build": [{"name": "valid"}], "build-extra": []}
        before = copy.deepcopy(original)
        normalized = catalog.validate_catalog(original)
        self.assertEqual(original, before)
        self.assertEqual(
            normalized["build"][0],
            {"name": "valid", "architecture": "dual", "deps": []},
        )

    def test_duplicate_json_keys_rejected(self) -> None:
        """Duplicate JSON keys in the catalog file raise ValueError."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text('{"build": [], "build": [], "build-extra": []}')
            with self.assertRaisesRegex(ValueError, "duplicate catalog field"):
                catalog.load_catalog(path)

    def test_catalog_defines_the_audited_go_sources(self) -> None:
        """Only the seven audited Go consumers require setup-go on known recipes."""
        definitions = list(catalog.iter_sources(catalog.load_catalog()))
        self.assertEqual(
            {source.name for source in definitions if source.go},
            {
                "amazon-cloudwatch-agent",
                "amazon-ssm-agent",
                "blackbox_exporter",
                "frr_exporter",
                "node_exporter",
                "podman",
                "telegraf",
            },
        )
        recipes = [source for source in definitions if source.group == "build"]
        self.assertEqual(sum(len(source.architectures) for source in recipes), 81)
        self.assertEqual(
            sum(len(source.architectures) for source in recipes if source.go), 14
        )

    def test_source_definitions_are_immutable_and_share_output_policy(self) -> None:
        """Execution and output enforcement consume the same normalized definition."""
        source = catalog.find_source("build-extra", "hvinfo")
        self.assertEqual(source.deps, ("gnat", "gprbuild"))
        self.assertEqual(source.timeout_minutes, 30)
        self.assertEqual(source.allowed_outputs("amd64"), {"amd64", "all"})
        self.assertEqual(source.allowed_outputs("arm64"), {"arm64"})
        with self.assertRaises(FrozenInstanceError):
            source.name = "changed"
        independent = catalog.find_source("build", "pyhumps")
        self.assertEqual(independent.allowed_outputs("amd64"), {"all"})
        with self.assertRaisesRegex(ValueError, "no arm64 build"):
            independent.allowed_outputs("arm64")

    def test_unknown_test_sources_preserve_the_previous_build_environment(self) -> None:
        """Uncatalogued recipes keep Go/default timeouts, standalone builds do not."""
        recipe = catalog.find_source("build", "uncatalogued-source")
        standalone = catalog.find_source("build-extra", "uncatalogued-source")
        self.assertEqual(recipe.build_settings(), {"go": True, "timeout_minutes": 150})
        self.assertEqual(standalone.build_settings(), {"timeout_minutes": 30})
        self.assertEqual(recipe.architectures, ("amd64", "arm64"))

    def test_execution_settings_are_strict_and_recipe_go_is_explicit(self) -> None:
        """Typos, boolean integers, extreme budgets and standalone Go are rejected."""
        for field, values in (
            ("go", ["true", 1, None]),
            ("priority", [-1, 101, True, "1", None]),
            ("timeout_minutes", [0, 361, True, "30", None]),
        ):
            for value in values:
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    catalog.validate_catalog(
                        {"build": [{"name": "new", field: value}], "build-extra": []}
                    )
        with self.assertRaisesRegex(ValueError, "recipe setting"):
            catalog.validate_catalog(
                {"build": [], "build-extra": [{"name": "new", "go": False}]}
            )

    def test_execution_settings_default_without_mutating_catalog_entries(self) -> None:
        """Only explicitly declared settings are persisted in normalized JSON entries."""
        sources = catalog.validate_catalog(
            {
                "build": [
                    {"name": "new", "go": True, "priority": 10, "timeout_minutes": 60}
                ],
                "build-extra": [],
            }
        )
        source = next(catalog.iter_sources(sources))
        self.assertEqual(source.priority, 10)
        self.assertEqual(source.build_settings(), {"go": True, "timeout_minutes": 60})
        self.assertEqual(sources["build"][0]["timeout_minutes"], 60)

    def test_add_scaffolds_a_valid_entry_without_changing_other_entries(self) -> None:
        """Future packages can be added from a validated CLI without editing workflows."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            original = {"build": [{"name": "existing"}], "build-extra": []}
            path.write_text(json.dumps(original))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    catalog.main(
                        [
                            "--catalog",
                            str(path),
                            "add",
                            "--group",
                            "build-extra",
                            "--name",
                            "new-package",
                            "--architecture",
                            "independent_only",
                            "--dep",
                            "libexample-dev",
                            "--timeout-minutes",
                            "45",
                        ]
                    ),
                    0,
                )
            result = json.loads(path.read_text())
            self.assertEqual(result["build"], original["build"])
            self.assertEqual(
                result["build-extra"],
                [
                    {
                        "name": "new-package",
                        "architecture": "independent_only",
                        "deps": ["libexample-dev"],
                        "timeout_minutes": 45,
                    }
                ],
            )
            self.assertEqual(
                catalog.find_source(
                    "build-extra", "new-package", catalog.load_catalog(path)
                ).architectures,
                ("amd64",),
            )

    def test_add_fails_without_writing_on_duplicates_or_bad_dependencies(self) -> None:
        """An invalid addition cannot rewrite an existing source or truncate its file."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text('{"build": [{"name": "existing"}], "build-extra": []}\n')
            before = path.read_bytes()
            for entry in ({"name": "existing"}, {"name": "new", "deps": ["--unsafe"]}):
                with self.subTest(entry=entry), self.assertRaises(ValueError):
                    catalog.add_source(path, "build-extra", entry)
                self.assertEqual(path.read_bytes(), before)

    def test_validate_cli_reports_expanded_native_builds(self) -> None:
        """The no-network validation command exposes effective package build settings."""
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(catalog.main(["validate"]), 0)
        self.assertIn("53 sources, 99 native builds", output.getvalue())
        self.assertIn(
            "build/linux-kernel: amd64,arm64; go=false; timeout=150m; priority=100",
            output.getvalue(),
        )
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            self.assertEqual(
                catalog.main(
                    ["--catalog", str(Path(temporary) / "missing"), "validate"]
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
