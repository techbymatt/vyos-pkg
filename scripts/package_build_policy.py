"""Plan and enforce amd64 ownership of architecture-independent packages.

The policy classifies source recipes, not binary package names. Output metadata
is checked on both fresh and restored builds to detect stale classifications.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from .package_catalog import (
        ARCHITECTURES,
        GROUPS,
        architecture_policy,
        architectures,
        find_source,
    )
except ImportError:
    from package_catalog import (
        ARCHITECTURES,
        GROUPS,
        architecture_policy,
        architectures,
        find_source,
    )

__all__ = ["architectures", "check_outputs", "independent_only"]


def independent_only(group: str, package: str) -> bool:
    """Report whether the source is classified independent_only."""
    return architecture_policy(group, package) == "independent_only"


def check_outputs(group: str, package: str, arch: str, directory: Path) -> None:
    """Verify each .deb in directory has an Architecture allowed by policy."""
    # Load/classify once per producer, not twice for every .deb it emits.
    allowed = find_source(group, package).allowed_outputs(arch)
    paths = sorted(directory.glob("*.deb"))
    if not paths:
        raise ValueError(f"{directory}: no .deb outputs")
    for path in paths:
        actual = subprocess.check_output(
            ["dpkg-deb", "--field", str(path), "Architecture"], text=True
        ).strip()
        if actual not in allowed:
            raise ValueError(
                f"{path}: Architecture {actual!r}, expected {sorted(allowed)}"
            )


def main(argv: list[str] | None = None) -> int:
    """CLI architectures/check subcommands; status: 0 success, 1 failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    arches = sub.add_parser("architectures")
    check = sub.add_parser("check")
    for command in (arches, check):
        command.add_argument("--group", choices=GROUPS, required=True)
        command.add_argument("--package", required=True)
    check.add_argument("--arch", choices=ARCHITECTURES, required=True)
    check.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "architectures":
            print(" ".join(architectures(args.group, args.package)))
        else:
            check_outputs(args.group, args.package, args.arch, args.directory)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"package_build_policy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
