"""Catalog membership, dependency migration and strict schema validation."""

import copy
import tempfile
import unittest
from pathlib import Path

try:
    from . import package_catalog as catalog
except ImportError:
    import package_catalog as catalog


class CatalogTests(unittest.TestCase):
    def test_catalog_preserves_publish_sources(self) -> None:
        sources = catalog.load_catalog()
        self.assertEqual(
            [entry["name"] for entry in sources["build"]],
            """
            amazon-cloudwatch-agent amazon-ssm-agent aws-gwlbtun bash-completion
            blackbox_exporter ddclient dropbear ethtool frr frr_exporter hostap
            hsflowd iproute2 isc-dhcp isc-kea keepalived libhtp libnss-mapuser
            libpam-radius-auth linux-kernel ndppd net-snmp netfilter node_exporter
            openssl openvpn openvpn-otp owamp podman pyhumps radvd shim-signed squid
            strongswan tacacs telegraf udp-broadcast-relay vyos-1x waagent wide-dhcpv6
            xen-guest-agent zerotier-one
        """.split(),
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
                "vyatta-bash": "bison libncurses5-dev",
                "vyatta-biosdevname": "libpci-dev",
                "vyatta-cfg": "bison flex libboost-filesystem-dev libglib2.0-dev",
                "vyos-http-api-tools": "dh-virtualenv",
                "vyos-live-build": "",
            },
        )

    def test_architecture_distinctions_and_unknown_sources(self) -> None:
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
        original = {"build": [{"name": "valid"}], "build-extra": []}
        before = copy.deepcopy(original)
        normalized = catalog.validate_catalog(original)
        self.assertEqual(original, before)
        self.assertEqual(
            normalized["build"][0],
            {"name": "valid", "architecture": "dual", "deps": []},
        )

    def test_duplicate_json_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text('{"build": [], "build": [], "build-extra": []}')
            with self.assertRaisesRegex(ValueError, "duplicate catalog field"):
                catalog.load_catalog(path)


if __name__ == "__main__":
    unittest.main()
