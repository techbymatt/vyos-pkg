"""Exercise the dependency mapping used by the publish planner."""

import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("package_dependencies.sh")


class PackageDependenciesTests(unittest.TestCase):
    def test_package_with_dependencies(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "hvinfo"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "gnat gprbuild\n")

    def test_package_without_dependencies(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "live-boot"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "\n")


if __name__ == "__main__":
    unittest.main()
