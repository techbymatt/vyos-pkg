"""Artifact names and ordered paths shared by builders, restore and Verify.

Do not reorder or narrow cache paths: GitHub includes them in cache versions,
including legacy gzip archives. Metadata intentionally spans the recipe tree.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .package_catalog import ARCHITECTURES, GROUPS, SOURCE_PATTERN, validate_name
except ImportError:
    from package_catalog import ARCHITECTURES, GROUPS, SOURCE_PATTERN, validate_name

ARTIFACT_PATTERN = re.compile(rf"deb-({SOURCE_PATTERN})-(amd64|arm64)")
METADATA_SUFFIXES = (
    "*.buildinfo",
    "*.changes",
    "*.dsc",
    "*debian.tar.*",
    "*orig.tar.*",
)


def artifact_name(package: str, arch: str) -> str:
    """Name a producer directory using the source name, not a binary identity."""
    validate_name(package)
    if arch not in ARCHITECTURES:
        raise ValueError(f"invalid producer architecture: {arch}")
    return f"deb-{package}-{arch}"


def artifact_arch(name: str) -> str:
    """Validate a producer directory name and return its native architecture."""
    match = ARTIFACT_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError(f"unknown artifact directory name: {name}")
    return match[2]


@dataclass(frozen=True)
class PackagePaths:
    """The ordered cache/upload contract for one build group and source."""

    directory: str
    metadata: str

    @property
    def debs(self) -> str:
        """The binary output glob."""
        return f"{self.directory}/*.deb"

    @property
    def sources(self) -> tuple[str, ...]:
        """Source/build metadata paths in their historical cache order."""
        return tuple(f"{self.metadata}/{suffix}" for suffix in METADATA_SUFFIXES)

    @property
    def cache(self) -> tuple[str, ...]:
        """All paths that define the cache version."""
        return (self.debs, *self.sources)

    def output_text(self) -> str:
        """Encode the composite action's fixed, validated GITHUB_OUTPUT values."""
        sources = "\n".join(self.sources)
        cache = "\n".join(self.cache)
        return (
            f"directory={self.directory}\ndebs={self.debs}\n"
            f"sources<<PATHS\n{sources}\nPATHS\ncache<<PATHS\n{cache}\nPATHS\n"
        )


def package_paths(group: str, package: str) -> PackagePaths:
    """Resolve paths without depending on catalog membership (manual Test)."""
    validate_name(package)
    if group == "build":
        return PackagePaths(
            f"vyos-build/scripts/package-build/{package}",
            "vyos-build/scripts/package-build/**",
        )
    if group == "build-extra":
        return PackagePaths("packages", "packages")
    raise ValueError(f"invalid package group: {group}")


def cleanup_outputs(paths: PackagePaths, root: Path) -> None:
    """Remove only matching output files, never source trees or external targets."""
    root = root.resolve()
    matches = {
        Path(match)
        for pattern in paths.cache
        for match in glob.glob(str(root / pattern), recursive=True)
    }
    # Validate the complete set first; symlinked parents must not escape the job.
    for path in matches:
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"output path escapes workspace: {path}")
    for path in sorted(matches):
        if path.is_file() or path.is_symlink():
            path.unlink()


def main(argv: list[str] | None = None) -> int:
    """Emit action paths, or clean files after one batched restore slot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("outputs", "clean"))
    parser.add_argument("--group", choices=GROUPS, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        paths = package_paths(args.group, args.package)
        if args.command == "clean":
            cleanup_outputs(paths, args.root)
        else:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(paths.output_text())
    except (OSError, ValueError, KeyError) as error:
        print(f"package_layout: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
