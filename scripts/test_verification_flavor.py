"""Test flavor preparation against a fixed image-builder configuration."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import verification_flavor as vf


class VerificationFlavorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.write(
            "scripts/image-build/defaults.py",
            "boot_settings = {'console_type': 'tty', 'console_num': '0', 'console_speed': '115200'}\n",
        )
        self.write("data/defaults.toml", 'build_type = "development"\n')
        self.write(
            "data/build-types/development.toml", 'packages = ["vyos-1x-smoketest"]\n'
        )
        self.write(
            "data/architectures/amd64.toml", '[boot_settings]\nconsole_type = "ttyS"\n'
        )
        self.write(
            "data/architectures/arm64.toml",
            '[boot_settings]\nconsole_type = "ttyAMA"\n',
        )
        self.write("data/build-flavors/generic.toml", 'image_format = "iso"\n')

    def write(self, path: str, content: str) -> None:
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_amd64_uses_architecture_and_upstream_defaults(self) -> None:
        self.assertEqual(
            vf.create_flavor(self.source, "amd64", "generic"),
            {
                "flavor": "generic",
                "console_type": "ttyS",
                "console_num": "0",
                "console_speed": "115200",
            },
        )

    def test_arm64_uses_architecture_and_upstream_defaults(self) -> None:
        self.assertEqual(
            vf.create_flavor(self.source, "arm64", "generic"),
            {
                "flavor": "generic",
                "console_type": "ttyAMA",
                "console_num": "0",
                "console_speed": "115200",
            },
        )

    def test_flavor_overrides_architecture_and_selects_build_type(self) -> None:
        self.write(
            "data/build-types/release.toml", '[boot_settings]\nconsole_speed = "9600"\n'
        )
        self.write(
            "data/build-flavors/custom.toml",
            'build_type = "release"\n[boot_settings]\nconsole_type = "ttyUSB"\nconsole_num = "2"\n',
        )
        self.assertEqual(
            vf.create_flavor(self.source, "arm64", "custom"),
            {
                "flavor": "custom",
                "console_type": "ttyUSB",
                "console_num": "2",
                "console_speed": "9600",
            },
        )

    def test_build_type_overrides_config_defaults(self) -> None:
        self.write(
            "data/defaults.toml",
            'build_type = "development"\n[boot_settings]\nconsole_speed = "57600"\n',
        )
        self.write(
            "data/build-types/development.toml",
            '[boot_settings]\nconsole_speed = "9600"\n',
        )
        self.assertEqual(
            vf.create_flavor(self.source, "amd64", "generic")["console_speed"], "9600"
        )

    def test_missing_console_number_is_rejected(self) -> None:
        self.write(
            "data/build-flavors/generic.toml", '[boot_settings]\nconsole_num = ""\n'
        )
        with self.assertRaisesRegex(ValueError, "console_num"):
            vf.create_flavor(self.source, "amd64", "generic")

    def test_invalid_console_speed_is_rejected(self) -> None:
        self.write(
            "data/build-flavors/generic.toml",
            '[boot_settings]\nconsole_speed = "fast"\n',
        )
        with self.assertRaisesRegex(ValueError, "console_speed"):
            vf.create_flavor(self.source, "arm64", "generic")

    def test_non_string_setting_is_rejected(self) -> None:
        self.write(
            "data/architectures/amd64.toml", "[boot_settings]\nconsole_num = 0\n"
        )
        with self.assertRaisesRegex(TypeError, "must be strings"):
            vf.create_flavor(self.source, "amd64", "generic")

    def test_missing_architecture_file_is_rejected(self) -> None:
        (self.source / "data/architectures/arm64.toml").unlink()
        with self.assertRaises(FileNotFoundError):
            vf.create_flavor(self.source, "arm64", "generic")

    def test_cli_writes_json_for_package_installation(self) -> None:
        output = self.source / "flavor.json"
        result = subprocess.run(
            [
                sys.executable,
                str(Path(vf.__file__)),
                "--source",
                str(self.source),
                "--arch",
                "arm64",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(output.read_text()),
            {
                "flavor": "generic",
                "console_type": "ttyAMA",
                "console_num": "0",
                "console_speed": "115200",
            },
        )


if __name__ == "__main__":
    unittest.main()
